# main.py
import os
import json
import logging
import asyncio
from datetime import datetime
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from collections import Counter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ---------- Config ----------
with open("data.json", encoding="utf-8") as f:
    DATA = json.load(f)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("TOKEN not set in environment")

ALLOWED_IDS = set(DATA["allowed_user_ids"])
ADMIN_IDS   = set(DATA["admin_ids"])
HR_CONTACTS = DATA["hr_contacts"]

bot = Bot(token=TOKEN)
dp  = Dispatcher()

PAGE_SIZE  = 7
STATS_FILE = "stats.json"

# ---------- Helpers ----------
def allowed(uid: int) -> bool:
    return uid in ALLOWED_IDS

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ---------- Stats ----------
def load_stats() -> dict[str, Counter]:
    if not os.path.exists(STATS_FILE):
        return {"helpful": Counter(), "not_helpful": Counter()}
    with open(STATS_FILE, encoding="utf-8") as f:
        raw = json.load(f)
    return {"helpful": Counter(raw["helpful"]), "not_helpful": Counter(raw["not_helpful"])}

def save_stats(stats: dict[str, Counter]) -> None:
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump({k: dict(v) for k, v in stats.items()}, f, ensure_ascii=False, indent=2)

stats = load_stats()

# ---------- Keyboard builders ----------
def paginate(items: list[str], page: int, prefix: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for idx, text in enumerate(items[start : start + PAGE_SIZE], start):
        kb.button(text=text, callback_data=f"{prefix}_{idx}")
    kb.adjust(1)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}_prev_{page - 1}"))
    if (page + 1) * PAGE_SIZE < len(items):
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}_next_{page + 1}"))
    if nav:
        kb.row(*nav)
    return kb.as_markup()

def main_menu_kb(uid: int) -> InlineKeyboardMarkup:
    kb = []
    if is_admin(uid):
        kb.append([InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")])
    kb.extend([
        [InlineKeyboardButton(text="📚 Категории вопросов", callback_data="categories_0")],
        [InlineKeyboardButton(text="📞 Контакты HR", callback_data="hr_contacts")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ---------- States ----------
user_states: dict[int, dict] = {}

# ---------- Handlers ----------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not allowed(msg.from_user.id):
        await msg.answer("❌ Доступ запрещён.")
        return
    await msg.answer("👋 Что вас интересует?", reply_markup=main_menu_kb(msg.from_user.id))

@dp.callback_query(lambda c: c.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    await callback.message.edit_text("👋 Что вас интересует?", reply_markup=main_menu_kb(callback.from_user.id))
    await callback.answer()

# --- Categories ---
@dp.callback_query(lambda c: c.data.startswith("categories_"))
async def show_categories(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return

    parts = callback.data.split("_")
    page = int(parts[-1]) if parts[-2] in {"prev", "next"} else int(parts[-1])

    cat_names = [
        c["name"] for c in DATA["categories"]
        if not c.get("admin_only", False) or is_admin(uid)
    ]
    await callback.message.edit_text(
        "📂 Выберите категорию:",
        reply_markup=paginate(cat_names, page, "category")
    )
    await callback.answer()

# --- Pick category ---
@dp.callback_query(lambda c: c.data.startswith("category_"))
async def pick_category(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return

    try:
        cat_idx = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.")
        return

    visible_categories = [
        c for c in DATA["categories"]
        if not c.get("admin_only", False) or is_admin(uid)
    ]
    if cat_idx >= len(visible_categories):
        await callback.answer("Категория не найдена.")
        return

    category = visible_categories[cat_idx]
    user_states[uid] = {"cat": category["id"]}  # сохраняем id категории

    titles = [q["question"] for q in category["questions"]]
    kb = paginate(titles, 0, "q")
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])
    await callback.message.edit_text(
        f"📂 *{category['name']}*\n\nВыберите вопрос:",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    await callback.answer()

# --- Show question ---
@dp.callback_query(lambda c: c.data.startswith("q_"))
async def show_question(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return

    try:
        q_idx = int(callback.data.split("_")[-1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка.")
        return

    state = user_states.get(uid)
    if not state or "cat" not in state:
        await callback.answer("Сессия устарела.")
        return

    category_id = state["cat"]
    category = next((c for c in DATA["categories"] if c["id"] == category_id), None)
    if not category:
        await callback.answer("Категория не найдена.")
        return

    questions = category["questions"]
    if q_idx >= len(questions):
        await callback.answer("Вопрос не найден.")
        return

    question = questions[q_idx]
    stats_key = f"{category_id}_{q_idx}"

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="👍 Полезно", callback_data=f"rate_1_{stats_key}")],
            [InlineKeyboardButton(text="👎 Не помогло", callback_data=f"rate_0_{stats_key}")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")],
        ]
    )
    await callback.message.edit_text(
        f"❓ *{question['question']}*\n\n{question['answer']}",
        parse_mode="Markdown",
        reply_markup=kb,
        disable_web_page_preview=True,
    )
    await callback.answer()

# --- Rate answer ---
@dp.callback_query(lambda c: c.data.startswith("rate_"))
async def rate_answer(callback: CallbackQuery):
    _, flag, key = callback.data.split("_", 2)
    if flag == "1":
        stats["helpful"][key] += 1
    else:
        stats["not_helpful"][key] += 1
    save_stats(stats)
    await callback.answer("Спасибо за обратную связь!")

# --- Admin stats ---
@dp.callback_query(lambda c: c.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    uid = callback.from_user.id
    if not is_admin(uid):
        await callback.answer("Нет доступа.")
        return

    if not stats["helpful"] and not stats["not_helpful"]:
        text = "📊 Статистика пуста."
    else:
        lines = ["📊 Статистика по ответам:"]
        for key in set(stats["helpful"]) | set(stats["not_helpful"]):
            cat_id, q_id = key.split("_")
            category = next(c for c in DATA["categories"] if str(c["id"]) == cat_id)
            question = category["questions"][int(q_id)]
            useful = stats["helpful"][key]
            useless = stats["not_helpful"][key]
            lines.append(f"{question['question'][:40]}… — 👍{useful} 👎{useless}")
        text = "\n".join(lines)

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

# --- HR Contacts ---
@dp.callback_query(lambda c: c.data == "hr_contacts")
async def show_hr_contacts(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    contacts = HR_CONTACTS
    text = (
        "📞 Контакты HR-отдела:\n\n"
        f"📧 Email: {contacts['email']}\n"
        f"📞 Телефон: {contacts['phone']}\n"
        f"💬 Telegram: {', '.join(contacts['telegram'])}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

# ---------- HTTP health check ----------
routes = web.RouteTableDef()

@routes.get("/")
async def health(request):
    return web.Response(text="OK")

async def run_http():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("HTTP server started on 0.0.0.0:%s", port)

# ---------- Entry point ----------
async def main():
    await run_http()
    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.error("Fatal error: %s", e)
