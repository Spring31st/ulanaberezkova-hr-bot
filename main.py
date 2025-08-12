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
with open('data.json', encoding='utf-8') as f:
    data = json.load(f)

TOKEN = os.getenv("TOKEN")
ALLOWED_IDS = set(data["allowed_user_ids"])
ADMIN_IDS = set(data["admin_ids"])
HR_CONTACTS = data["hr_contacts"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

PAGE_SIZE = 7
STATS_FILE = "stats.json"
REMINDERS_FILE = "reminders.json"

# ---------- Helpers ----------
def allowed(uid: int) -> bool:
    return uid in ALLOWED_IDS

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ---------- Stats ----------
def load_stats() -> dict[str, Counter]:
    if not os.path.exists(STATS_FILE):
        return {"helpful": Counter(), "not_helpful": Counter()}
    with open(STATS_FILE, encoding='utf-8') as f:
        raw = json.load(f)
    return {
        "helpful": Counter(raw["helpful"]),
        "not_helpful": Counter(raw["not_helpful"])
    }

def save_stats(stats: dict[str, Counter]) -> None:
    with open(STATS_FILE, 'w', encoding='utf-8') as f:
        json.dump({k: dict(v) for k, v in stats.items()}, f, ensure_ascii=False, indent=2)

stats = load_stats()

# ---------- Reminders ----------
def load_reminders() -> dict[int, list[dict]]:
    if not os.path.exists(REMINDERS_FILE):
        return {}
    with open(REMINDERS_FILE, encoding='utf-8') as f:
        raw = json.load(f)
    return {int(uid): lst for uid, lst in raw.items()}

def save_reminders(reminders: dict[int, list[dict]]) -> None:
    with open(REMINDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2)

reminders = load_reminders()

class IdCounter:
    def __init__(self, start: int):
        self._value = start
    def next(self) -> int:
        val = self._value
        self._value += 1
        return val

next_remind_id = IdCounter(
    max([r["id"] for lst in reminders.values() for r in lst], default=0) + 1
)

# ---------- Background worker ----------
async def reminder_worker():
    await asyncio.sleep(5)
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        for uid, lst in list(reminders.items()):
            still_active = []
            for r in lst:
                if datetime.strptime(r["dt_str"], "%d.%m.%Y %H:%M") <= now:
                    try:
                        await bot.send_message(uid, f"🔔 *Напоминание:*\n{r['text']}", parse_mode="Markdown")
                    except Exception as e:
                        logging.warning(f"Remind send failed to {uid}: {e}")
                else:
                    still_active.append(r)
            reminders[uid] = still_active
        save_reminders({k: v for k, v in reminders.items() if v})

# ---------- Keyboard builders ----------
def paginate(items: list[str], page: int, prefix: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    start = page * PAGE_SIZE
    for idx, text in enumerate(items[start: start + PAGE_SIZE], start):
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
        [InlineKeyboardButton(text="📅 Создать напоминание", callback_data="remind_start")],
        [InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders")]
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

@dp.callback_query(lambda c: c.data.startswith("categories_"))
async def show_categories(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return

    parts = callback.data.split("_")
    if parts[1] in ("prev", "next"):
        page = int(parts[2])
    else:
        page = int(parts[1])

    cat_names = [
        c["name"] for c in data["categories"]
        if not c.get("admin_only", False) or is_admin(uid)
    ]

    # ✅ Ключевое исправление: используем префикс "category" (без s), а не "categories"
    await callback.message.edit_text(
        "📂 Выберите категорию:",
        reply_markup=paginate(cat_names, page, "category") 
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("category_"))
async def pick_category(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return

    try:
        cat_idx = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка обработки категории.")
        return

    categories = [
        c for c in data["categories"]
        if not c.get("admin_only", False) or is_admin(uid)
    ]
    if cat_idx >= len(categories):
        await callback.answer("Ошибка категории.")
        return

    category = categories[cat_idx]
    user_states[uid] = {"cat": category["id"]}

    question_titles = [q["question"] for q in category["questions"]]
    kb = paginate(question_titles, 0, "q")  # префикс "q" для вопросов
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])

    await callback.message.edit_text(
        f"📂 *{category['name']}*\n\nВыберите вопрос:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await callback.answer()
