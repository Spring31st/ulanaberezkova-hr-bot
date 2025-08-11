import os
import json
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncio
from aiohttp import web

# ---------- настройки ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

with open('data.json', encoding='utf-8') as f:
    data = json.load(f)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    logger.error("TOKEN is missing")
    exit(1)

ALLOWED_IDS = data.get("allowed_user_ids", [])
HR_CONTACTS = data.get("hr_contacts", {})
bot = Bot(token=TOKEN)
dp = Dispatcher()

user_states = {}
PAGE_SIZE = 7
PARSE_MODE = "Markdown"

# ---------- HTTP ----------
routes = web.RouteTableDef()
@routes.get('/')
async def health(request): return web.Response(text="OK")
async def run_http():
    app = web.Application(); app.add_routes(routes)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 10000))).start()

# ---------- пагинация ----------
def paginate(items: list[str], page: int, prefix: str):
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for idx, text in enumerate(items[start:start + PAGE_SIZE], start):
        kb.button(text=text, callback_data=f"{prefix}_{idx}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}_prev_{page-1}"))
    if start + PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}_next_{page+1}"))
    if nav:
        kb.row(*nav)
    return kb.as_markup()

def allowed(uid): return uid in ALLOWED_IDS

# ---------- /start ----------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not allowed(msg.from_user.id):
        await msg.answer("❌ Доступ запрещён."); return
    cat_names = [c["name"] for c in data["categories"]]
    await msg.answer("👋 Выберите категорию:", reply_markup=paginate(cat_names, 0, "cat"))

# ---------- выбор категории ----------
@dp.callback_query(lambda c: c.data.startswith("cat_"))
async def pick_category(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return

    if callback.data.startswith(("cat_prev_", "cat_next_")):
        _, _, _, page = callback.data.split("_")
        cat_names = [c["name"] for c in data["categories"]]
        await callback.message.edit_reply_markup(
            reply_markup=paginate(cat_names, int(page), "cat")
        )
        await callback.answer(); return

    cat_idx = int(callback.data.split("_")[1])
    category = data["categories"][cat_idx]
    user_states[uid] = category["id"]

    q_titles = [q["question"] for q in category["questions"]]
    await callback.message.edit_text(
        f"📂 *{category['name']}*\n\nВыберите вопрос:",
        parse_mode=PARSE_MODE,
        reply_markup=paginate(q_titles, 0, "q")
    )
    await callback.answer()

# ---------- выбор вопроса ----------
@dp.callback_query(lambda c: c.data.startswith("q_"))
async def pick_question(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return

    if callback.data.startswith(("q_prev_", "q_next_")):
        _, _, _, page = callback.data.split("_")
        cat_id = user_states.get(uid)
        category = next(c for c in data["categories"] if c["id"] == cat_id)
        q_titles = [q["question"] for q in category["questions"]]
        await callback.message.edit_reply_markup(
            reply_markup=paginate(q_titles, int(page), "q")
        )
        await callback.answer(); return

    q_idx = int(callback.data.split("_")[1])
    cat_id = user_states[uid]
    category = next(c for c in data["categories"] if c["id"] == cat_id)
    question = category["questions"][q_idx]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 Помог", callback_data="helpful_yes"),
         InlineKeyboardButton(text="👎 Не помог", callback_data="helpful_no")],
        [InlineKeyboardButton(text="🔙 Все категории", callback_data="back_to_cats")]
    ])

    await callback.message.answer(
        question["answer"],
        parse_mode=PARSE_MODE,
        reply_markup=kb
    )
    await callback.answer()

# ---------- обратная связь ----------
@dp.callback_query(lambda c: c.data in {"helpful_yes", "helpful_no"})
async def feedback(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Все категории", callback_data="back_to_cats")]
    ])

    if callback.data == "helpful_yes":
        await callback.message.answer(
            "✅ *Спасибо за обратную связь!*",
            parse_mode=PARSE_MODE,
            reply_markup=kb
        )
    else:
        contacts = "\n".join([f"{v}" for v in HR_CONTACTS.values()])
        await callback.message.answer(
            f"😔 *К сожалению, не смог помочь.*\n\n{contacts}",
            parse_mode=PARSE_MODE,
            reply_markup=kb
        )
    await callback.answer()

# ---------- возврат к категориям ----------
@dp.callback_query(lambda c: c.data == "back_to_cats")
async def back_to_categories(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return
    user_states[uid] = None
    cat_names = [c["name"] for c in data["categories"]]
    await callback.message.answer(
        "👋 Выберите категорию:",
        reply_markup=paginate(cat_names, 0, "cat")
    )
    await callback.answer()

# ---------- запуск ----------
async def main():
    await asyncio.gather(run_http(), dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    asyncio.run(main())
