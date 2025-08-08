import os
import json
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, Message
from aiogram.filters import Command
import asyncio
from aiohttp import web

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Загрузка данных из файла data.json
try:
    with open('data.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    logger.info(f"✅ Загружено {len(data['categories'])} категорий")
except Exception as e:
    logger.error(f"❌ Ошибка загрузки data.json: {e}")
    raise

# Получение токена бота из переменных окружения
TOKEN = os.getenv("TOKEN", "")
if not TOKEN:
    logger.error("TOKEN is missing")
    exit(1)

ALLOWED_IDS = data.get("allowed_user_ids", [])
user_states = {}

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Настройка HTTP-сервера для UptimeRobot
routes = web.RouteTableDef()

@routes.get('/')
async def health(request):
    return web.Response(text="OK")

async def run_http():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"HTTP listening on 0.0.0.0:{port}")

# Основной функционал бота
def get_menu(user_id):
    current = user_states.get(user_id)
    if current is None:
        buttons = [[KeyboardButton(text=cat["name"])] for cat in data["categories"]]
    else:
        category = next((c for c in data["categories"] if c["id"] == current), None)
        if not category:
            user_states[user_id] = None
            return get_menu(user_id)
        buttons = [[KeyboardButton(text=q["question"])] for q in category["questions"]]
        buttons.append([KeyboardButton(text="🔙 Назад к категориям")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def allowed(uid):
    return uid in ALLOWED_IDS

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        await msg.answer("❌ Доступ запрещён.")
        return
    user_states[uid] = None
    await msg.answer("👋 Здравствуйте! Выберите категорию:", reply_markup=get_menu(uid))

@dp.message()
async def handle(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        await msg.answer("❌ Доступ запрещён.")
        return
    text = msg.text.strip()

    if text == "🔙 Назад к категориям":
        user_states[uid] = None
        await msg.answer("Выберите категорию:", reply_markup=get_menu(uid))
        return

    cur = user_states.get(uid)
    if cur is None:  # Выбор категории
        for cat in data["categories"]:
            if cat["name"] == text:
                user_states[uid] = cat["id"]
                await msg.answer(f"Категория: {text}\n\nВыберите вопрос:", reply_markup=get_menu(uid))
                return
        await msg.answer("❌ Неизвестная категория.", reply_markup=get_menu(uid))
    else:  # Выбор вопроса
        category = next((c for c in data["categories"] if c["id"] == cur), None)
        if not category:
            user_states[uid] = None
            await msg.answer("❌ Ошибка. Вернитесь в главное меню.", reply_markup=get_menu(uid))
            return
        for q in category["questions"]:
            if q["question"] == text:
                await msg.answer(q["answer"])
                return
        await msg.answer("❌ Неизвестный вопрос.", reply_markup=get_menu(uid))

async def main():
    await asyncio.gather(run_http(), dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    asyncio.run(main())
