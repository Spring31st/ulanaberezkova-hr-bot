import os
import json
import logging
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
import asyncio
from aiohttp import web
from collections import Counter

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
ADMIN_IDS = data.get("admin_ids", [])
HR_CONTACTS = data.get("hr_contacts", {})
bot = Bot(token=TOKEN)
dp = Dispatcher()

user_states = {}
PAGE_SIZE = 7
PARSE_MODE = "Markdown"
STATS_FILE = "stats.json"

# ---------- статистика ----------
def load_stats():
    if not os.path.exists(STATS_FILE):
        return {"helpful": Counter(), "not_helpful": Counter()}
    with open(STATS_FILE, encoding='utf-8') as f:
        raw = json.load(f)
    return {"helpful": Counter(raw["helpful"]),
            "not_helpful": Counter(raw["not_helpful"])}

def save_stats(stats):
    with open(STATS_FILE, 'w', encoding='utf-8') as f:
        json.dump({k: dict(v) for k, v in stats.items()}, f, ensure_ascii=False, indent=2)

stats = load_stats()

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
def is_admin(uid): return uid in ADMIN_IDS

# ---------- /start ----------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not allowed(msg.from_user.id):
        await msg.answer("❌ Доступ запрещён."); return
    cat_names = [c["name"] for c in data["categories"]]
    await msg.answer("👋 Выберите категорию:", reply_markup=paginate(cat_names, 0, "cat"))

# ---------- /stats ----------
@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Команда доступна только админам."); return

    not_help = stats["not_helpful"]
    if not not_help:
        await msg.answer("📊 Пока ни одного «не помог»."); return

    top = not_help.most_common(5)
    lines = [f"{idx+1}. {q} — {cnt}" for idx, (q, cnt) in enumerate(top)]
    await msg.answer("📉 ТОП-5 «не помог»:\n" + "\n".join(lines))

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
    user_states[uid] = {"cat": category["id"]}

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
        cat_id = user_states.get(uid, {}).get("cat")
        category = next(c for c in data["categories"] if c["id"] == cat_id)
        q_titles = [q["question"] for q in category["questions"]]
        await callback.message.edit_reply_markup(
            reply_markup=paginate(q_titles, int(page), "q")
        )
        await callback.answer(); return

    q_idx = int(callback.data.split("_")[1])
    cat_id = user_states[uid]["cat"]
    category = next(c for c in data["categories"] if c["id"] == cat_id)
    question = category["questions"][q_idx]

    user_states[uid]["q"] = question["id"]

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
    uid = callback.from_user.id
    q = user_states.get(uid, {}).get("q", "unknown")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Все категории", callback_data="back_to_cats")]
    ])

    if callback.data == "helpful_yes":
        stats["helpful"][str(q)] += 1
        text = "✅ *Спасибо за обратную связь!*"
    else:
contacts = (
    "📞 *HR-отдел:*\n"
    f"📧 {HR_CONTACTS.get('email', '')}\n"
    f"📞 {HR_CONTACTS.get('phone', '')}\n"
    "\n".join([f"💬 {t}" for t in HR_CONTACTS.get("telegram", [])])
        )
        text = f"😔 *К сожалению, не смог помочь.*\n\n{contacts}"

    save_stats(stats)
    await callback.message.answer(text, parse_mode=PARSE_MODE, reply_markup=kb)
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
