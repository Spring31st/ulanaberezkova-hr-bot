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

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ---------- Config ----------
with open('data.json', encoding='utf-8') as f:
    data = json.load(f)

TOKEN = os.getenv("TOKEN")
ALLOWED_IDS = set(data["allowed_user_ids"])
ADMIN_IDS   = set(data["admin_ids"])
HR_CONTACTS = data["hr_contacts"]

bot = Bot(token=TOKEN)
dp = Dispatcher()

PAGE_SIZE = 7
STATS_FILE = "stats.json"
REMINDERS_FILE = "reminders.json"

# ---------- Helpers ----------
def allowed(uid: int) -> bool: return uid in ALLOWED_IDS
def is_admin(uid: int) -> bool: return uid in ADMIN_IDS

# ---------- Stats ----------
def load_stats() -> dict[str, Counter]:
    if not os.path.exists(STATS_FILE):
        return {"helpful": Counter(), "not_helpful": Counter()}
    with open(STATS_FILE, encoding='utf-8') as f:
        raw = json.load(f)
    return {"helpful": Counter(raw["helpful"]),
            "not_helpful": Counter(raw["not_helpful"])}

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

# Atomic counter for reminder IDs
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
    await asyncio.sleep(5)   # let polling start
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
        reminders.update({k: v for k, v in reminders.items() if v})
        save_reminders(reminders)

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
        [InlineKeyboardButton(text="📚 Категории вопросов", callback_data="cat_0")],
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

@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("❌ Команда доступна только админам.")
        return
    not_help = stats["not_helpful"]
    if not not_help:
        await msg.answer("📊 Пока ни одного «не помог».")
        return
    top = not_help.most_common(5)
    lines = [f"{idx + 1}. {q} — {cnt}" for idx, (q, cnt) in enumerate(top, 1)]
    await msg.answer("📉 ТОП-5 «не помог»:\n" + "\n".join(lines))

@dp.callback_query(lambda c: c.data.startswith("cat_"))
async def pick_category(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    if callback.data.startswith(("cat_prev_", "cat_next_")):
        _, _, page = callback.data.split("_")
        cat_names = [
            c["name"] for c in data["categories"]
            if not c.get("admin_only") or is_admin(uid)
        ]
        await callback.message.edit_reply_markup(reply_markup=paginate(cat_names, int(page), "cat"))
        return await callback.answer()
    cat_idx = int(callback.data.split("_")[1])
    categories = [
        c for c in data["categories"]
        if not c.get("admin_only") or is_admin(uid)
    ]
    if cat_idx >= len(categories):
        await callback.answer("Ошибка категории.")
        return
    category = categories[cat_idx]
    user_states[uid] = {"cat": category["id"]}
    kb = paginate([q["question"] for q in category["questions"]], 0, "q")
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])
    await callback.message.edit_text(
        f"📂 *{category['name']}*\n\nВыберите вопрос:",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("q_"))
async def pick_question(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    if callback.data.startswith(("q_prev_", "q_next_")):
        _, _, page = callback.data.split("_")
        cat_id = user_states.get(uid, {}).get("cat")
        questions = next((c for c in data["categories"] if c["id"] == cat_id), {}).get("questions", [])
        q_titles = [q["question"] for q in questions]
        await callback.message.edit_reply_markup(reply_markup=paginate(q_titles, int(page), "q"))
        return await callback.answer()
    q_idx = int(callback.data.split("_")[1])
    cat_id = user_states[uid]["cat"]
    questions = next((c for c in data["categories"] if c["id"] == cat_id), {}).get("questions", [])
    if q_idx >= len(questions):
        await callback.answer("Ошибка вопроса.")
        return
    question = questions[q_idx]
    user_states[uid]["q"] = question["id"]
    kb_rows = [
        [
            InlineKeyboardButton(text="👍 Помог", callback_data="helpful_yes"),
            InlineKeyboardButton(text="👎 Не помог", callback_data="helpful_no")
        ],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    ]
    if question.get("remind"):
        kb_rows.insert(
            1,
            [InlineKeyboardButton(text="⏰ Напомнить", callback_data=f"remind_auto_{question['remind_text']}")]
        )
    await callback.message.answer(question["answer"], parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()

@dp.callback_query(lambda c: c.data in {"helpful_yes", "helpful_no"})
async def feedback(callback: CallbackQuery):
    uid = callback.from_user.id
    q = user_states.get(uid, {}).get("q", "unknown")
    if callback.data == "helpful_yes":
        stats["helpful"][str(q)] += 1
        text = "✅ *Спасибо за обратную связь!*"
    else:
        stats["not_helpful"][str(q)] += 1
        lines = ["😔 *К сожалению, не смог помочь.*", "", "📞 *HR-отдел:*"]
        if HR_CONTACTS.get("email"):
            lines.append(f"📧 *E-mail:* {HR_CONTACTS['email']}")
        if HR_CONTACTS.get("phone"):
            lines.append(f"📞 *Телефон:* {HR_CONTACTS['phone']}")
        for tg in HR_CONTACTS.get("telegram", []):
            if tg:
                lines.append(f"💬 *Telegram:* {tg}")
        text = "\n".join(lines)
    save_stats(stats)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]])
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    uid = callback.from_user.id
    if uid not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    not_help = stats["not_helpful"]
    if not not_help:
        txt = "📊 Пока ни одного «не помог»."
    else:
        top = not_help.most_common(5)
        txt = "📉 ТОП-5 «не помог»:\n" + "\n".join(f"{i}. {q} — {cnt}" for i, (q, cnt) in enumerate(top, 1))
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]])
    await callback.message.edit_text(txt, reply_markup=kb)
    await callback.answer()

# ---------- Reminder flows ----------
@dp.callback_query(lambda c: c.data == "remind_start")
async def remind_start(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    await callback.message.edit_text(
        "📅 Введите дату отправки напоминания в формате ДД.ММ.ГГГГ:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
        )
    )
    user_states[uid] = {"wait_remind": "date"}
    await callback.answer()

@dp.callback_query(lambda c: c.data == "list_reminders")
async def list_reminders(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    lst = reminders.get(uid, [])
    if not lst:
        await callback.message.edit_text(
            "📭 У вас нет активных напоминаний.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
            )
        )
        return await callback.answer()
    kb_rows = []
    for r in lst:
        kb_rows.append([
            InlineKeyboardButton(text=f"{r['dt_str']} – {r['text'][:30]}", callback_data="noop"),
            InlineKeyboardButton(text="❌", callback_data=f"delrem_{r['id']}")
        ])
    kb_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])
    await callback.message.edit_text("📋 Ваши напоминания:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("delrem_"))
async def del_remind(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    rid = int(callback.data.split("_")[1])
    reminders[uid] = [r for r in reminders.get(uid, []) if r["id"] != rid]
    reminders = {k: v for k, v in reminders.items() if v}
    save_reminders(reminders)
    await callback.answer("🗑 Удалено!")
    await list_reminders(callback)

@dp.callback_query(lambda c: c.data.startswith("remind_auto_"))
async def remind_auto(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    text = callback.data.replace("remind_auto_", "")
    await callback.message.edit_text(
        f"📅 Введите дату напоминания «{text}» (ДД.ММ.ГГГГ):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
        )
    )
    user_states[uid] = {"wait_remind": "date", "remind_auto_text": text}
    await callback.answer()

@dp.callback_query(lambda c: c.data == "noop")
async def noop(callback: CallbackQuery):
    await callback.answer()

@dp.message()
async def handle_remind(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid):
        return
    state = user_states.get(uid, {}).get("wait_remind")
    if state == "date":
        try:
            datetime.strptime(msg.text, "%d.%m.%Y")
        except ValueError:
            await msg.answer("❗️ Неверный формат. Введите ДД.ММ.ГГГГ:")
            return
        user_states[uid]["wait_remind"] = "time"
        user_states[uid]["remind_date"] = msg.text
        await msg.answer("⏰ Введите время ЧЧ:ММ:")
    elif state == "time":
        try:
            datetime.strptime(msg.text, "%H:%M")
        except ValueError:
            await msg.answer("❗️ Неверный формат. Введите ЧЧ:ММ:")
            return
        user_states[uid]["wait_remind"] = "text"
        user_states[uid]["remind_time"] = msg.text
        text = user_states[uid].get("remind_auto_text", "Напомнить")
        await msg.answer(f"📝 Подтвердите текст:\n{text}")
    elif state == "text":
        dt_str = f"{user_states[uid]['remind_date']} {user_states[uid]['remind_time']}"
        if datetime.strptime(dt_str, "%d.%m.%Y %H:%M") <= datetime.now():
            await msg.answer("❗️ Укажите будущую дату и время.")
            return
        text = user_states[uid].get("remind_auto_text", msg.text)
        reminders.setdefault(uid, []).append(
            {"id": next_remind_id.next(), "dt_str": dt_str, "text": text}
        )
        save_reminders(reminders)
        del user_states[uid]["wait_remind"]
        await msg.answer("✅ Напоминание сохранено!", reply_markup=main_menu_kb(uid))

# ---------- HTTP health check ----------
routes = web.RouteTableDef()

@routes.get('/')
async def health(request):
    return web.Response(text="OK")

async def run_http():
    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', int(os.getenv("PORT", 10000))).start()

# ---------- Entry point ----------
async def main():
    asyncio.create_task(reminder_worker())
    await asyncio.gather(run_http(), dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    asyncio.run(main())
