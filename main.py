import os
import json
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, Message,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import Command
import asyncio
from aiohttp import web

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
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

# Контакты HR
HR_CONTACTS = data.get("hr_contacts", {
    "email": "📧 hr@company.com",
    "phone": "📞 +7 (495) 123-45-67",
    "telegram": "💬 @hr_support"
})

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

def get_feedback_keyboard():
    """Создаёт инлайн-клавиатуру для обратной связи"""
    keyboard = [
        [
            InlineKeyboardButton(text="👍 Помог", callback_data="helpful_yes"),
            InlineKeyboardButton(text="👎 Не помог", callback_data="helpful_no")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def format_hr_contacts():
    """Форматирует контакты HR в красивый вид"""
    return (
        "📞 **HR-отдел:**\n"
        f"{HR_CONTACTS.get('email', '')}\n"
        f"{HR_CONTACTS.get('phone', '')}\n"
        f"{HR_CONTACTS.get('telegram', '')}"
    )

def allowed(uid):
    return uid in ALLOWED_IDS

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        await msg.answer("❌ Доступ запрещён.")
        return
    user_states[uid] = None
    await msg.answer(
        "👋 Здравствуйте! Выберите категорию:",
        reply_markup=get_menu(uid)
    )

@dp.message()
async def handle(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        await msg.answer("❌ Доступ запрещён.")
        return
    
    text = msg.text.strip()

    if text == "🔙 Назад к категориям":
        user_states[uid] = None
        await msg.answer(
            "Выберите категорию:",
            reply_markup=get_menu(uid)
        )
        return

    cur = user_states.get(uid)
    if cur is None:  # Выбор категории
        for cat in data["categories"]:
            if cat["name"] == text:
                user_states[uid] = cat["id"]
                await msg.answer(
                    f"📂 Категория: {text}\n\nВыберите вопрос:",
                    reply_markup=get_menu(uid)
                )
                return
        await msg.answer(
            "❌ Неизвестная категория.",
            reply_markup=get_menu(uid)
        )
    else:  # Выбор вопроса
        category = next((c for c in data["categories"] if c["id"] == cur), None)
        if not category:
            user_states[uid] = None
            await msg.answer(
                "❌ Ошибка. Вернитесь в главное меню.",
                reply_markup=get_menu(uid)
            )
            return
        
        for q in category["questions"]:
            if q["question"] == text:
                await msg.answer(
                    q["answer"],
                    reply_markup=get_feedback_keyboard()
                )
                return
        
        await msg.answer(
            "❌ Неизвестный вопрос.",
            reply_markup=get_menu(uid)
        )

@dp.callback_query()
async def handle_callback(callback_query: CallbackQuery):
    """Обработчик нажатий на инлайн-кнопки обратной связи"""
    uid = callback_query.from_user.id
    if not allowed(uid):
        await callback_query.answer("Доступ запрещён", show_alert=True)
        return
    
    message = callback_query.message
    data = callback_query.data
    
    if data == "helpful_yes":
        # Удаляем клавиатуру и добавляем подтверждение
        await message.edit_text(
            f"{message.text}\n\n✅ **Спасибо за обратную связь!**",
            parse_mode="HTML"
        )
        await callback_query.answer("Спасибо!")
        
    elif data == "helpful_no":
        # Добавляем контакты HR
        contacts = format_hr_contacts()
        await message.edit_text(
            f"{message.text}\n\n😔 **К сожалению, не смог помочь**\n\n{contacts}",
            parse_mode="HTML"
        )
        await callback_query.answer("Контакты HR отправлены")

async def main():
    await asyncio.gather(run_http(), dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    asyncio.run(main())
