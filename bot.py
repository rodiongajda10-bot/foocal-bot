import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

# --- МИНИ-СЕРВЕР ДЛЯ RENDER ---
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), SimpleHandler)
    server.serve_forever()

# Запускаем веб-сервер в фоновом потоке
threading.Thread(target=run_server, daemon=True).start()
# ---------------------------------------------

# Инициализация бота
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Простой хэндлер на /start
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer("Привет! Я твой бот-калорийщик. Отправь мне фото еды!")

async def main():
    print("Starting bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
