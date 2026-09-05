import asyncio
import datetime
import logging
import os
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not GEMINI_API_KEY:
    raise RuntimeError(
        "Не найдены BOT_TOKEN и/или GEMINI_API_KEY. Проверь файл .env"
    )

client = genai.Client(api_key=GEMINI_API_KEY)
MODEL = "gemini-3.6-flash"

# Твой ID администратора (безлимит)
ADMIN_ID = 436772123

SYSTEM_PROMPT = (
    "Ты — помощник фитнес-бота, который оценивает еду по фото. "
    "Определи блюдо на фото, оцени размер порции на глаз и дай оценку "
    "калорийности и БЖУ (белки/жиры/углеводы) для всей порции, видимой на фото. "
    "Если что-то не видно точно (соус, масло, скрытые ингредиенты) — заложи это "
    "в оценку как разумное предположение. "
    "Выдай ответ строго в таком формате:\n"
    "Блюдо: [Название]\n"
    "Порция: [Оценка размера, например ~350 г]\n"
    "Калории: [Число] ккал\n"
    "Белки: [Число] г\n"
    "Жиры: [Число] г\n"
    "Углеводы: [Число] г\n"
    "Точность: [высокая / средняя / низкая] (если точность средняя или низкая, обязательно в скобках кратко укажи причину и что уточнить, например: (не виден объем масла/соуса, напиши граммовку))\n"
    "Примечания: [Короткие пояснения или допущения]"
)

# Настройка базы данных SQLite
conn = sqlite3.connect("users.db")
cursor = conn.cursor()

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    requests_count INTEGER DEFAULT 0,
    daily_goal INTEGER DEFAULT 2000
)
"""
)

try:
    cursor.execute(
        "ALTER TABLE users ADD COLUMN daily_goal INTEGER DEFAULT 2000"
    )
    conn.commit()
except sqlite3.OperationalError:
    pass

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS user_sessions (
    user_id INTEGER PRIMARY KEY,
    last_analysis TEXT,
    corrections_left INTEGER DEFAULT 3
)
"""
)

cursor.execute(
    """
CREATE TABLE IF NOT EXISTS food_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    date TEXT,
    calories INTEGER,
    meal_info TEXT
)
"""
)
conn.commit()

MAX_FREE_REQUESTS = 10


class GoalStates(StatesGroup):
    waiting_for_gender = State()
    waiting_for_age = State()
    waiting_for_weight = State()
    waiting_for_height = State()
    waiting_for_activity = State()
    waiting_for_goal_type = State()


def get_user_data(user_id: int):
    cursor.execute(
        "SELECT requests_count, daily_goal FROM users WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO users (user_id, requests_count, daily_goal) VALUES (?, 0, 2000)",
            (user_id,),
        )
        conn.commit()
        return 0, 2000
    return row[0], row[1]


def check_and_increment_limit(user_id: int, max_free: int = 10) -> bool:
    if user_id == ADMIN_ID:
        return True

    requests_count, _ = get_user_data(user_id)
    if requests_count >= max_free:
        return False

    cursor.execute(
        "UPDATE users SET requests_count = requests_count + 1 WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    return True


def add_requests_to_user(user_id: int, amount: int):
    requests_count, _ = get_user_data(user_id)
    new_count = max(0, requests_count - amount)
    cursor.execute(
        "UPDATE users SET requests_count = ? WHERE user_id = ?",
        (new_count, user_id),
    )
    conn.commit()


def set_user_goal_calories(user_id: int, calories: int):
    cursor.execute(
        "UPDATE users SET daily_goal = ? WHERE user_id = ?", (calories, user_id)
    )
    conn.commit()


def add_food_log(user_id: int, calories: int, meal_info: str):
    today = datetime.date.today().isoformat()
    cursor.execute(
        "INSERT INTO food_history (user_id, date, calories, meal_info) VALUES (?, ?, ?, ?)",
        (user_id, today, calories, meal_info),
    )
    conn.commit()


def get_today_consumed_calories(user_id: int) -> int:
    today = datetime.date.today().isoformat()
    cursor.execute(
        "SELECT SUM(calories) FROM food_history WHERE user_id = ? AND date = ?",
        (user_id, today),
    )
    row = cursor.fetchone()
    return row[0] if row and row[0] else 0


logging.basicConfig(level=logging.INFO)
bot = Bot(
    token=TELEGRAM_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN),
)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    welcome_text = (
        "Привет! 👋 Я **Foocal** — твой персональный фитнес-помощник и анализатор еды.\n\n"
        "🥗 **Что я умею:**\n"
        "1. Рассчитывать калории и БЖУ по фото еды.\n"
        "2. Вести дневной трекер и показывать твой прогресс.\n"
        "3. Подбирать индивидуальную суточную норму калорий.\n"
        "4. Корректировать расчеты по твоим сообщениям в чате.\n\n"
        f"🎁 У тебя есть **{MAX_FREE_REQUESTS} бесплатных запросов**. Отправь фото блюда прямо сейчас, чтобы начать! 👇"
    )
    await message.answer(welcome_text)


@dp.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "💡 **Справка по использованию Foocal:**\n\n"
        "• **Фото еды:** отправляй четкие фото порций для анализа.\n"
        "• **Коррекция:** если ИИ не учел соус или масло, просто напиши об этом следующим сообщением.\n"
        "• **Цель (/goal):** рассчитай свою суточную норму калорий под похудение, набор или поддержание веса.\n"
        "• **Баланс (/balance):** проверяй оставшиеся запросы и дневной прогресс калорий.\n\n"
        "Остались вопросы? Нажми /support и напиши нам!"
    )
    await message.answer(help_text)


@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    user_id = message.from_user.id
    used, daily_goal = get_user_data(user_id)
    left = max(0, MAX_FREE_REQUESTS - used)

    today_kcal = get_today_consumed_calories(user_id)
    remaining_kcal = daily_goal - today_kcal

    balance_text = (
        f"📊 **Твоя статистика и баланс:**\n\n"
        f"• Бесплатные запросы: использовано **{used}** из {MAX_FREE_REQUESTS} (осталось: **{left}**)\n"
        f"• Дневная цель: **{daily_goal} ккал**\n"
        f"• Съедено сегодня: **{today_kcal} ккал**\n"
    )

    if remaining_kcal >= 0:
        balance_text += (
            f"• Осталось до нормы: **{remaining_kcal} ккал** 🟢\n\n"
        )
    else:
        balance_text += (
            f"• Превышение нормы: **{abs(remaining_kcal)} ккал** 🔴\n\n"
        )

    balance_text += "💳 Купить пакет запросов: /buy\n🎯 Настроить цель: /goal"
    await message.answer(balance_text)


@dp.message(Command("goal"))
async def cmd_goal_start(message: Message, state: FSMContext):
    await state.set_state(GoalStates.waiting_for_gender)
    await message.answer(
        "🎯 **Расчет суточной нормы калорий**\n\n"
        "Укажи свой пол (напиши **М** или **Ж**):"
    )


@dp.message(GoalStates.waiting_for_gender)
async def process_gender(message: Message, state: FSMContext):
    text = message.text.strip().upper()
    if text not in ["М", "Ж", "МУЖСКОЙ", "ЖЕНСКИЙ", "M", "F"]:
        await message.answer("⚠️ Пожалуйста, укажи корректно: **М** или **Ж**.")
        return

    gender = "male" if text in ["М", "МУЖСКОЙ", "M"] else "female"
    await state.update_data(gender=gender)
    await state.set_state(GoalStates.waiting_for_age)
    await message.answer("Сколько тебе лет? (введи число, например: `16`)")


@dp.message(GoalStates.waiting_for_age)
async def process_age(message: Message, state: FSMContext):
    try:
        age = int(message.text.strip())
        if not (5 < age < 120):
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Введи реальный возраст числом (например, `16`).")
        return

    await state.update_data(age=age)
    await state.set_state(GoalStates.waiting_for_weight)
    await message.answer("Какой твой вес в кг? (например: `65` или `65.5`)")


@dp.message(GoalStates.waiting_for_weight)
async def process_weight(message: Message, state: FSMContext):
    try:
        weight = float(message.text.strip().replace(",", "."))
        if not (20 < weight < 300):
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Введи вес числом в килограммах.")
        return

    await state.update_data(weight=weight)
    await state.set_state(GoalStates.waiting_for_height)
    await message.answer("Какой твой рост в см? (например: `175`)")


@dp.message(GoalStates.waiting_for_height)
async def process_height(message: Message, state: FSMContext):
    try:
        height = float(message.text.strip().replace(",", "."))
        if not (50 < height < 250):
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Введи рост числом в сантиметрах.")
        return

    await state.update_data(height=height)
    await state.set_state(GoalStates.waiting_for_activity)
    await message.answer(
        "🏋️‍♂️ **Уровень физической активности:**\n"
        "Напиши цифру от 1 до 4:\n"
        "1 — Минимальная (сидячий образ жизни)\n"
        "2 — Низкая (легкие тренировки 1-3 раза в неделю)\n"
        "3 — Умеренная (тренировки 3-5 раз в неделю)\n"
        "4 — Высокая (интенсивные тренировки каждый день)"
    )


@dp.message(GoalStates.waiting_for_activity)
async def process_activity(message: Message, state: FSMContext):
    text = message.text.strip()
    activity_map = {"1": 1.2, "2": 1.375, "3": 1.55, "4": 1.725}
    if text not in activity_map:
        await message.answer(
            "⚠️ Пожалуйста, выбери цифру от 1 до 4 из списка."
        )
        return

    await state.update_data(activity=activity_map[text])
    await state.set_state(GoalStates.waiting_for_goal_type)
    await message.answer(
        "🎯 **Какая твоя главная цель?**\n"
        "Напиши цифру:\n"
        "1 — **Похудение** (дефицит калорий)\n"
        "2 — **Поддержание веса**\n"
        "3 — **Набор массы** (профицит калорий)"
    )


@dp.message(GoalStates.waiting_for_goal_type)
async def process_goal_type(message: Message, state: FSMContext):
    text = message.text.strip()
    if text not in ["1", "2", "3"]:
        await message.answer("⚠️ Выбери цифру 1, 2 или 3.")
        return

    data = await state.get_data()
    gender = data["gender"]
    age = data["age"]
    weight = data["weight"]
    height = data["height"]
    activity = data["activity"]

    if gender == "male":
        bmr = 10 * weight + 6.25 * height - 5 * age + 5
    else:
        bmr = 10 * weight + 6.25 * height - 5 * age - 161

    tdee = bmr * activity

    if text == "1":
        target_calories = int(tdee - 400)
        goal_name = "Похудение (дефицит)"
    elif text == "2":
        target_calories = int(tdee)
        goal_name = "Поддержание веса"
    else:
        target_calories = int(tdee + 300)
        goal_name = "Набор массы"

    set_user_goal_calories(message.from_user.id, target_calories)
    await state.clear()

    await message.answer(
        f"✅ **Цель успешно настроена!**\n\n"
        f"• Цель: **{goal_name}**\n"
        f"• Твоя суточная норма: **{target_calories} ккал**\n\n"
        "Теперь каждое отправленное фото еды будет автоматически вычитаться из этого лимита. Прогресс можно посмотреть командой /balance!"
    )


@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    prices = [LabeledPrice(label="Пакет 30 запросов", amount=1)]
    await bot.send_invoice(
        chat_id=message.chat.id,
        title="Пакет анализов еды (30 запросов)",
        description="Пополнение баланса запросов для ИИ-анализатора Foocal 🥗",
        payload="package_30_requests",
        currency="XTR",
        prices=prices,
    )


@dp.pre_checkout_query()
async def pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    user_id = message.from_user.id
    payment_info = message.successful_payment

    if payment_info.invoice_payload == "package_30_requests":
        add_requests_to_user(user_id, 30)
        await message.answer(
            "🎉 **Оплата прошла успешно!**\n\n"
            "Тебе добавлено **+30 запросов**. Можешь продолжать пользоваться ботом! 🚀"
        )


@dp.message(Command("support"))
async def cmd_support(message: Message):
    support_text = (
        "🛠 **Поддержка Foocal**\n\n"
        "Напиши свое сообщение или вопрос следующим сообщением следующим образом прямо сюда в чат, и администратор ответит тебе!"
    )
    await message.answer(support_text)


@dp.message(
    F.chat.type == "private",
    ~F.text.startswith("/"),
    F.from_user.id != ADMIN_ID,
    ~F.photo,
)
async def handle_text_messages(message: Message):
    user_id = message.from_user.id

    cursor.execute(
        "SELECT last_analysis, corrections_left FROM user_sessions WHERE user_id = ?",
        (user_id,),
    )
    row = cursor.fetchone()

    if row and row[0]:
        last_analysis, corrections_left = row[0], row[1]

        if corrections_left <= 0:
            await message.answer(
                "❌ Лимит исправлений для этого фото исчерпан (максимум 3 раза).\n"
                "Отправь новое фото блюда, чтобы начать сначала! 📸"
            )
            return

        await bot.send_chat_action(
            chat_id=message.chat.id, action=ChatAction.TYPING
        )

        correction_prompt = (
            f"Ранее ты дал следующий расчет блюда:\n{last_analysis}\n\n"
            f"Пользователь прислал исправление/уточнение: '{message.text}'.\n"
            "Пожалуйста, учти это исправление и выдай полностью пересчитанный "
            "и исправленный результат строго в том же формате:\n"
            "Блюдо: [Название]\n"
            "Порция: [Оценка размера]\n"
            "Калории: [Число] ккал\n"
            "Белки: [Число] г\n"
            "Жиры: [Число] г\n"
            "Углеводы: [Число] г\n"
            "Точность: [высокая / средняя / низкая] (если точность средняя или низкая, обязательно в скобках кратко укажи причину и что уточнить, например: (не виден объем масла/соуса, напиши граммовку))\n"
            "Примечания: [Пояснение с учетом исправления]"
        )

        try:
            response = client.models.generate_content(
                model=MODEL, contents=[correction_prompt]
            )
            new_text = response.text

            cursor.execute(
                "UPDATE user_sessions SET last_analysis = ?, corrections_left = corrections_left - 1 WHERE user_id = ?",
                (new_text, user_id),
            )
            conn.commit()

            left_attempts = corrections_left - 1
            await message.answer(
                f"{new_text}\n\n*(Осталось попыток исправления для этого фото: {left_attempts}/3)*"
            )
        except Exception as e:
            logging.error(f"Ошибка при корректировке через Gemini API: {e}")
            await message.answer(
                "⚠️ Не удалось обработать исправление. Попробуй еще раз."
            )
        return

    forwarded = await bot.forward_message(
        chat_id=ADMIN_ID,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    await bot.send_message(
        ADMIN_ID,
        f"📩 Сообщение от пользователя [ID: `{message.from_user.id}`]\n"
        f"Чтобы ответить, сделай **Reply (Ответить)** на это сообщение.",
    )
    await message.answer(
        "✅ Твое сообщение отправлено в службу поддержки! Ожидай ответа."
    )


@dp.message(
    F.chat.type == "private",
    ~F.text.startswith("/"),
    F.from_user.id == ADMIN_ID,
    ~F.reply_to_message,
)
async def admin_chat_warning(message: Message):
    await message.answer(
        "ℹ️ Это чат поддержки. Ты администратор! Чтобы ответить пользователю, сделай **Reply (Ответить)** на пересланное тебе сообщение с его вопросом."
    )


@dp.message(F.chat.type == "private", F.from_user.id == ADMIN_ID, F.reply_to_message)
async def answer_from_admin(message: Message):
    reply_msg = message.reply_to_message
    if not reply_msg:
        return

    try:
        if reply_msg.forward_from:
            target_user_id = reply_msg.forward_from.id
        else:
            text_lines = reply_msg.text.split("\n") if reply_msg.text else []
            target_user_id = None
            for line in text_lines:
                if "ID:" in line:
                    clean_id = (
                        line.replace("ID:", "")
                        .replace("`", "")
                        .replace("[", "")
                        .replace("]", "")
                        .strip()
                    )
                    target_user_id = int(clean_id)

        if target_user_id:
            await bot.send_message(
                target_user_id,
                f"🛠 **Ответ от поддержки Foocal:**\n\n{message.text}",
            )
            await message.react([{"type": "emoji", "emoji": "👍"}])
        else:
            await message.answer(
                "⚠️ Не удалось определить ID пользователя для ответа."
            )
    except Exception as e:
        logging.error(f"Ошибка отправки ответа пользователю: {e}")
        await message.answer("⚠️ Не удалось отправить сообщение пользователю.")


@dp.message(F.photo)
async def handle_photo(message: Message):
    user_id = message.from_user.id

    if not check_and_increment_limit(user_id, max_free=MAX_FREE_REQUESTS):
        await message.answer(
            "❌ У тебя закончились бесплатные запросы (10/10).\n\n"
            "💳 Чтобы купить пакет из 30 дополнительных запросов за Telegram Stars, нажми команду /buy"
        )
        return

    await bot.send_chat_action(
        chat_id=message.chat.id, action=ChatAction.TYPING
    )

    photo = message.photo[-1]
    file_info = await bot.get_file(photo.file_id)
    file_path = file_info.file_path

    downloaded_file = await bot.download_file(file_path)
    image_bytes = downloaded_file.read()

    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                SYSTEM_PROMPT,
            ],
        )
        analysis_result = response.text

        dish_calories = 0
        for line in analysis_result.split("\n"):
            if "Калории:" in line:
                import re

                numbers = re.findall(r"\d+", line)
                if numbers:
                    dish_calories = int(numbers[0])
                break

        add_food_log(user_id, dish_calories, analysis_result[:50])

        cursor.execute(
            """
            INSERT OR REPLACE INTO user_sessions (user_id, last_analysis, corrections_left)
            VALUES (?, ?, 3)
        """,
            (user_id, analysis_result),
        )
        conn.commit()

        _, daily_goal = get_user_data(user_id)
        today_total = get_today_consumed_calories(user_id)
        remaining = daily_goal - today_total

        progress_text = (
            f"\n\n📈 **Дневной прогресс:**\n"
            f"• Съедено сегодня: **{today_total} / {daily_goal} ккал**\n"
        )
        if remaining >= 0:
            progress_text += f"• Осталось до нормы: **{remaining} ккал** 🟢"
        else:
            progress_text += (
                f"• Превышение нормы: **{abs(remaining)} ккал** 🔴"
            )

        await message.answer(
            f"{analysis_result}\n{progress_text}\n\n*(💡 Если нужно учесть другой ингредиент, отправь правку сообщением. Осталось попыток: 3/3)*"
        )
    except Exception as e:
        logging.error(f"Ошибка при обращении к Gemini API: {e}")
        await message.answer(
            "⚠️ Не получилось проанализировать фото. Попробуй еще раз чуть позже."
        )


async def set_main_menu(bot: Bot):
    commands = [
        BotCommand(
            command="start",
            description="Перезапустить бота и узнать правила 🚀",
        ),
        BotCommand(
            command="help",
            description="Как правильно делать фото для анализа 💡",
        ),
        BotCommand(
            command="balance", description="Проверить оставшиеся запросы 📊"
        ),
        BotCommand(
            command="goal", description="Настроить суточную норму калорий 🎯"
        ),
        BotCommand(
            command="support", description="Написать в службу поддержки 🛠"
        ),
    ]
    await bot.set_my_commands(commands)


async def main():
    logging.info("Бот запущен!")
    await set_main_menu(bot)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())