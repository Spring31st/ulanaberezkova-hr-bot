import os
import json
import logging
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from collections import Counter, defaultdict

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
ADMIN_IDS   = data.get("admin_ids", [])
HR_CONTACTS = data.get("hr_contacts", {})

bot = Bot(token=TOKEN)
dp = Dispatcher(storage=MemoryStorage())   # MemoryStorage для FSM

user_states = {}
PAGE_SIZE = 7
PARSE_MODE = "Markdown"
STATS_FILE = "stats.json"
REMINDERS_FILE = "reminders.json"

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

# ---------- reminders ----------
def load_reminders() -> dict:
    if not os.path.exists(REMINDERS_FILE):
        return {}
    with open(REMINDERS_FILE, encoding='utf-8') as f:
        raw = json.load(f)
    return {int(uid): lst for uid, lst in raw.items()}

def save_reminders(reminders):
    with open(REMINDERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(reminders, f, ensure_ascii=False, indent=2, default=str)

reminders = load_reminders()
next_remind_id = max(
    [r["id"] for lst in reminders.values() for r in lst], default=0
) + 1

async def reminder_worker():
    """Раз в минуту отправляет готовые напоминания."""
    global reminders, next_remind_id
    while True:
        now = datetime.now()
        to_delete = []
        for uid, lst in reminders.items():
            still_active = []
            for r in lst:
                dt = datetime.strptime(r["dt_str"], "%d.%m.%Y %H:%M")
                if dt <= now:
                    try:
                        await bot.send_message(
                            uid,
                            f"🔔 *Напоминание:*\n{r['text']}",
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось отправить напоминание {r['id']} юзеру {uid}: {e}")
                    to_delete.append((uid, r["id"]))
                else:
                    still_active.append(r)
            reminders[uid] = still_active
        # удаляем отправленные
        for uid, rid in to_delete:
            reminders[uid] = [r for r in reminders.get(uid, []) if r["id"] != rid]
        reminders = {k: v for k, v in reminders.items() if v}
        save_reminders(reminders)
        await asyncio.sleep(60)

# ---------- HTTP ----------
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

def allowed(uid):  return uid in ALLOWED_IDS
def is_admin(uid): return uid in ADMIN_IDS

# ---------- главное меню ----------
def main_menu_kb(user_id: int) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="📚 HR-справка", callback_data="hr_faq")],
        [InlineKeyboardButton(text="📅 Создать напоминание", callback_data="create_remind_step1")],
        [InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders")]
    ]
    if user_id in ADMIN_IDS:
        kb.insert(0, [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ---------- команды ----------
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not allowed(msg.from_user.id):
        await msg.answer("❌ Доступ запрещён.")
        return
    await msg.answer("👋 Что вас интересует?", reply_markup=main_menu_kb(msg.from_user.id))

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
    lines = [f"{idx+1}. {q} — {cnt}" for idx, (q, cnt) in enumerate(top)]
    await msg.answer("📉 ТОП-5 «не помог»:\n" + "\n".join(lines))

# ---------- FSM для создания напоминания ----------
class RemindFSM(StatesGroup):
    date = State()
    time = State()
    text = State()

# ---------- колбэки главного меню ----------
@dp.callback_query(lambda c: c.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    if not allowed(callback.from_user.id):
        return
    await callback.message.edit_text(
        "👋 Что вас интересует?",
        reply_markup=main_menu_kb(callback.from_user.id)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    not_help = stats["not_helpful"]
    if not not_help:
        text = "📊 Пока ни одного «не помог»."
    else:
        top = not_help.most_common(5)
        lines = [f"{idx+1}. {q} — {cnt}" for idx, (q, cnt) in enumerate(top)]
        text = "📉 ТОП-5 «не помог»:\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
    )
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()

# ---------- HR FAQ ----------
@dp.callback_query(lambda c: c.data == "hr_faq")
async def cb_hr_faq(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    cat_names = [c["name"] for c in data["categories"]]
    kb = paginate(cat_names, 0, "cat")
    kb.inline_keyboard.append(
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    )
    await callback.message.edit_text("📚 Выберите категорию:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cat_"))
async def pick_category(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    if callback.data.startswith(("cat_prev_", "cat_next_")):
        _, _, _, page = callback.data.split("_")
        cat_names = [c["name"] for c in data["categories"]]
        await callback.message.edit_reply_markup(
            reply_markup=paginate(cat_names, int(page), "cat")
        )
        await callback.answer()
        return

    cat_idx = int(callback.data.split("_")[1])
    category = data["categories"][cat_idx]
    user_states[uid] = {"cat": category["id"]}

    q_titles = [q["question"] for q in category["questions"]]
    kb = paginate(q_titles, 0, "q")
    kb.inline_keyboard.append(
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    )
    await callback.message.edit_text(
        f"📂 *{category['name']}*\n\nВыберите вопрос:",
        parse_mode=PARSE_MODE,
        reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("q_"))
async def pick_question(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid):
        return
    if callback.data.startswith(("q_prev_", "q_next_")):
        _, _, _, page = callback.data.split("_")
        cat_id = user_states.get(uid, {}).get("cat")
        category = next(c for c in data["categories"] if c["id"] == cat_id)
        q_titles = [q["question"] for q in category["questions"]]
        await callback.message.edit_reply_markup(
            reply_markup=paginate(q_titles, int(page), "q")
        )
        await callback.answer()
        return

    q_idx = int(callback.data.split("_")[1])
    cat_id = user_states[uid]["cat"]
    category = next(c for c in data["categories"] if c["id"] == cat_id)
    question = category["questions"][q_idx]
    user_states[uid]["q"] = question["id"]

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👍 Помог", callback_data="helpful_yes"),
         InlineKeyboardButton(text="👎 Не помог", callback_data="helpful_no")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
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

    if callback.data == "helpful_yes":
        stats["helpful"][str(q)] += 1
        text = "✅ *Спасибо за обратную связь!*"
    else:
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

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
    )
    await callback.message.answer(text, parse_mode=PARSE_MODE, reply_markup=kb)
    await callback.answer()

# ---------- создание напоминания ----------
@dp.callback_query(lambda c: c.data == "create_remind_step1")
async def create_remind_step1(callback: CallbackQuery, state: FSMContext):
    if not allowed(callback.from_user.id):
        return
    await callback.message.edit_text(
        "📅 Введите дату отправки напоминания в формате ДД.ММ.ГГГГ:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
        )
    )
    await state.set_state(RemindFSM.date)
    await callback.answer()

@dp.message(RemindFSM.date)
async def create_remind_date(msg: Message, state: FSMContext):
    try:
        datetime.strptime(msg.text, "%d.%m.%Y")
    except ValueError:
        await msg.answer("❗️ Неверный формат. Введите ДД.ММ.ГГГГ:")
        return
    await state.update_data(date=msg.text)
    await msg.answer("⏰ Введите время в формате ЧЧ:ММ (например 09:30):")
    await state.set_state(RemindFSM.time)

@dp.message(RemindFSM.time)
async def create_remind_time(msg: Message, state: FSMContext):
    try:
        datetime.strptime(msg.text, "%H:%M")
    except ValueError:
        await msg.answer("❗️ Неверный формат. Введите ЧЧ:ММ:")
        return
    await state.update_data(time=msg.text)
    await msg.answer("📝 Введите текст напоминания:")
    await state.set_state(RemindFSM.text)

@dp.message(RemindFSM.text)
async def create_remind_text(msg: Message, state: FSMContext):
    data = await state.get_data()
    dt_str = f"{data['date']} {data['time']}"
    try:
        dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")
    except ValueError:
        await msg.answer("❗️ Ошибка даты/времени, попробуйте ещё раз.")
        await state.clear()
        return
    if dt <= datetime.now():
        await msg.answer("❗️ Укажите будущую дату и время.")
        await state.clear()
        return
    global next_remind_id
    uid = msg.from_user.id
    reminders.setdefault(uid, []).append(
        {"id": next_remind_id, "dt_str": dt_str, "text": msg.text}
    )
    next_remind_id += 1
    save_reminders(reminders)
    await msg.answer("✅ Напоминание сохранено!", reply_markup=main_menu_kb(uid))
    await state.clear()

# ---------- просмотр/удаление напоминаний ----------
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
        await callback.answer()
        return
    kb_rows = []
    for r in lst:
        kb_rows.append([
            InlineKeyboardButton(text=f"{r['dt_str']} – {r['text'][:30]}", callback_data="noop"),
            InlineKeyboardButton(text="❌", callback_data=f"delrem_{r['id']}")
        ])
    kb_rows.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])
    await callback.message.edit_text(
        "📋 Ваши напоминания:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
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
    await list_reminders(callback)  # обновляем список

# ---------- запуск ----------
async def main():
    asyncio.create_task(reminder_worker())
    await asyncio.gather(run_http(), dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    asyncio.run(main())
