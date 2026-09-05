import os 
import sys
import logging
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv

# Загружаем переменные окружения (для локального запуска, на Render они берутся из настроек)
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Проверка токенов
if not TOKEN:
    print("Ошибка: Не задан TELEGRAM_BOT_TOKEN!")
    sys.exit(1)

if not GEMINI_API_KEY:
    print("Ошибка: Не задан GEMINI_API_KEY!")
    sys.exit(1)

# Инициализация бота и клиента Gemini
bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Используем актуальную модель Gemini
GEMINI_MODEL = "gemini-2.0-flash"
client = genai.Client(api_key=GEMINI_API_KEY)

logging.basicConfig(level=logging.INFO)

# Хранилище лимитов запросов (в памяти)
user_requests = {}


class Form(StatesGroup):
    waiting_for_weight = State()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user_requests[user_id] = user_requests.get(user_id, 0)
    
    welcome_text = (
        "🤖 **Что я умею:**\n"
        "1. Рассчитывать калории и БЖУ по фото еды.\n"
        "2. Вести дневной трекер и показывать твой прогресс.\n"
        "3. Подбирать индивидуальную суточную норму калорий.\n"
        "4. Корректировать расчеты по твоим сообщениям в чате.\n\n"
        "🎁 У тебя есть 10 бесплатных запросов. Отправь фото блюда прямо сейчас, чтобы начать! 👇"
    )
    await message.answer(welcome_text, parse_mode="Markdown")


@dp.message(F.photo)
async def handle_photo(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Проверка лимитов (10 бесплатных запросов)
    requests_made = user_requests.get(user_id, 0)
    if requests_made >= 10:
        await message.answer("⚠️ У тебя закончились бесплатные запросы.")
        return

    # Информируем пользователя, что бот думает
    processing_msg = await message.answer("🔍 Анализирую фото блюда...")

    try:
        # Получаем самое качественное фото
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        
        # Скачиваем файл изображения в байты прямо из Телеграма
        file_bytes_io = await bot.download_file(file_info.file_path)
        image_bytes = file_bytes_io.read()

        # Формируем промпт для Gemini
        prompt = (
            "Ты — профессиональный диетолог и нутрициолог. "
            "Проанализируй это фото еды. Определи блюдо, рассчитай его примерный вес, "
            "общие калории, а также БЖУ (белки, жиры, углеводы). "
            "Напиши ответ красиво и структурировано на русском языке."
        )

        # Отправляем запрос в Gemini с использованием правильного синтаксиса Google GenAI SDK
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=[
                genai_types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg",
                ),
                prompt
            ]
        )

        # Увеличиваем счетчик запросов
        user_requests[user_id] = requests_made + 1

        # Удаляем сообщение о загрузке и отправляем результат
        await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        await message.answer(response.text)

    except Exception as e:
        logging.error(f"Ошибка при обработке фото через Gemini API: {e}")
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=processing_msg.message_id)
        except:
            pass
        await message.answer(f"⚠️ Не получилось проанализировать фото.\nОшибка: `{e}`", parse_mode="Markdown")


async def main():
    print("Бот успешно запущен и ожидает сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
