import os, json, logging, asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from collections import Counter

logging.basicConfig(level=logging.INFO)
with open('data.json', encoding='utf-8') as f:
    data = json.load(f)

TOKEN = os.getenv("TOKEN")
ALLOWED_IDS   = data["allowed_user_ids"]
ADMIN_IDS     = data["admin_ids"]
HR_CONTACTS   = data["hr_contacts"]
bot = Bot(token=TOKEN)
dp = Dispatcher()

user_states = {}
PAGE_SIZE = 7
STATS_FILE = "stats.json"
REMINDERS_FILE = "reminders.json"

def allowed(uid): return uid in ALLOWED_IDS
def is_admin(uid): return uid in ADMIN_IDS

# --- stats & reminders helpers (коротко) ---
def load_stats():
    return {"helpful": Counter(), "not_helpful": Counter()} if not os.path.exists(STATS_FILE) else \
           {"helpful": Counter(json.load(open(STATS_FILE, encoding='utf-8'))["helpful"]),
            "not_helpful": Counter(json.load(open(STATS_FILE, encoding='utf-8'))["not_helpful"])}
def save_stats(stats):
    json.dump({k: dict(v) for k, v in stats.items()}, open(STATS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
stats = load_stats()

def load_reminders():
    return {int(uid): lst for uid, lst in json.load(open(REMINDERS_FILE, encoding='utf-8')).items()} if os.path.exists(REMINDERS_FILE) else {}
def save_reminders(reminders):
    json.dump(reminders, open(REMINDERS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
reminders = load_reminders()
next_remind_id = max([r["id"] for lst in reminders.values() for r in lst], default=0) + 1

async def reminder_worker():
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        for uid, lst in list(reminders.items()):
            still_active = [r for r in lst if datetime.strptime(r["dt_str"], "%d.%m.%Y %H:%M") > now]
            for r in lst:
                if datetime.strptime(r["dt_str"], "%d.%m.%Y %H:%M") <= now:
                    try:
                        await bot.send_message(uid, f"🔔 *Напоминание:*\n{r['text']}", parse_mode="Markdown")
                    except Exception as e:
                        logging.warning(e)
            reminders[uid] = still_active
        reminders = {k: v for k, v in reminders.items() if v}
        save_reminders(reminders)

def paginate(items, page, prefix):
    kb = InlineKeyboardBuilder()
    for idx, text in enumerate(items[page*PAGE_SIZE:page*PAGE_SIZE+PAGE_SIZE], page*PAGE_SIZE):
        kb.button(text=text, callback_data=f"{prefix}_{idx}")
    kb.adjust(1)
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"{prefix}_prev_{page-1}"))
    if page*PAGE_SIZE+PAGE_SIZE < len(items): nav.append(InlineKeyboardButton(text="➡️", callback_data=f"{prefix}_next_{page+1}"))
    if nav: kb.row(*nav)
    return kb.as_markup()

def main_menu_kb(uid): return InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")] if uid in ADMIN_IDS else None,
    [InlineKeyboardButton(text="📚 Категории вопросов", callback_data="cat_0")],
    [InlineKeyboardButton(text="📅 Создать напоминание", callback_data="remind_start")],
    [InlineKeyboardButton(text="📋 Мои напоминания", callback_data="list_reminders")]
])

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not allowed(msg.from_user.id): return await msg.answer("❌ Доступ запрещён.")
    await msg.answer("👋 Что вас интересует?", reply_markup=main_menu_kb(msg.from_user.id))

@dp.callback_query(lambda c: c.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    if not allowed(callback.from_user.id): return
    await callback.message.edit_text("👋 Что вас интересует?", reply_markup=main_menu_kb(callback.from_user.id))
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cat_"))
async def pick_category(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return
    if callback.data.startswith(("cat_prev_", "cat_next_")):
        _, _, _, page = callback.data.split("_")
        cat_names = [c["name"] for c in data["categories"] if not c.get("admin_only") or uid in ADMIN_IDS]
        await callback.message.edit_reply_markup(reply_markup=paginate(cat_names, int(page), "cat"))
        return await callback.answer()
    cat_idx = int(callback.data.split("_")[1])
    categories = [c for c in data["categories"] if not c.get("admin_only") or uid in ADMIN_IDS]
    category = categories[cat_idx]
    user_states[uid] = {"cat": category["id"]}
    kb = paginate([q["question"] for q in category["questions"]], 0, "q")
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")])
    await callback.message.edit_text(f"📂 *{category['name']}*\n\nВыберите вопрос:", parse_mode=PARSE_MODE, reply_markup=kb)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("q_"))
async def pick_question(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return
    if callback.data.startswith(("q_prev_", "q_next_")):
        _, _, _, page = callback.data.split("_")
        q_titles = [q["question"] for q in next(c for c in data["categories"] if c["id"]==user_states[uid]["cat"])["questions"]]
        await callback.message.edit_reply_markup(reply_markup=paginate(q_titles, int(page), "q"))
        return await callback.answer()
    q_idx = int(callback.data.split("_")[1])
    cat_id = user_states[uid]["cat"]
    question = next(c for c in data["categories"] if c["id"]==cat_id)["questions"][q_idx]
    user_states[uid]["q"] = question["id"]
    kb_rows = [
        [InlineKeyboardButton(text="👍 Помог", callback_data="helpful_yes"),
         InlineKeyboardButton(text="👎 Не помог", callback_data="helpful_no")],
        [InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]
    ]
    if question.get("remind"):
        kb_rows.insert(1, [InlineKeyboardButton(
            text="⏰ Напомнить",
            callback_data=f"remind_auto_{question['remind_text']}"
        )])
    await callback.message.answer(question["answer"], parse_mode=PARSE_MODE, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await callback.answer()

@dp.callback_query(lambda c: c.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery): ...

@dp.callback_query(lambda c: c.data == "remind_start")
async def remind_start(callback: CallbackQuery): ...

@dp.callback_query(lambda c: c.data == "list_reminders")
async def list_reminders(callback: CallbackQuery): ...

@dp.callback_query(lambda c: c.data.startswith("delrem_"))
async def del_remind(callback: CallbackQuery):
    global reminders
    uid = callback.from_user.id
    if not allowed(uid): return
    rid = int(callback.data.split("_")[1])
    reminders[uid] = [r for r in reminders.get(uid, []) if r["id"] != rid]
    reminders = {k: v for k, v in reminders.items() if v}
    save_reminders(reminders)
    await callback.answer("🗑 Удалено!")
    await list_reminders(callback)

@dp.callback_query(lambda c: c.data.startswith("remind_auto_"))
async def remind_auto(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid): return
    text = callback.data.replace("remind_auto_", "")
    await callback.message.edit_text(
        f"📅 Введите дату напоминания «{text}» (ДД.ММ.ГГГГ):",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
        )
    )
    user_states[uid].update({"wait_remind": "date", "remind_auto_text": text})
    await callback.answer()

@dp.message()
async def handle_remind(msg: Message):
    uid = msg.from_user.id
    if not allowed(uid): return
    state = user_states.get(uid, {}).get("wait_remind")
    if state == "date":
        try:
            datetime.strptime(msg.text, "%d.%m.%Y")
        except:
            await msg.answer("❗️ Неверный формат. Введите ДД.ММ.ГГГГ:")
            return
        user_states[uid]["wait_remind"] = "time"
        user_states[uid]["remind_date"] = msg.text
        await msg.answer("⏰ Введите время ЧЧ:ММ:")
    elif state == "time":
        try:
            datetime.strptime(msg.text, "%H:%M")
        except:
            await msg.answer("❗️ Неверный формат. Введите ЧЧ:ММ:")
            return
        user_states[uid]["wait_remind"] = "text"
        user_states[uid]["remind_time"] = msg.text
        text = user_states[uid]["remind_auto_text"]
        await msg.answer(f"📝 Подтвердите текст:\n{text}")
    elif state == "text":
        dt_str = f"{user_states[uid]['remind_date']} {user_states[uid]['remind_time']}"
        if datetime.strptime(dt_str, "%d.%m.%Y %H:%M") <= datetime.now():
            await msg.answer("❗️ Укажите будущую дату и время.")
            return
        text = user_states[uid]["remind_auto_text"]
        reminders.setdefault(uid, []).append(
            {"id": next_remind_id, "dt_str": dt_str, "text": text}
        )
        next_remind_id += 1
        save_reminders(reminders)
        del user_states[uid]["wait_remind"]
        await msg.answer("✅ Напоминание сохранено!", reply_markup=main_menu_kb(uid))

async def main():
    asyncio.create_task(reminder_worker())
    await asyncio.gather(run_http(), dp.start_polling(bot, skip_updates=True))

if __name__ == "__main__":
    asyncio.run(main())
