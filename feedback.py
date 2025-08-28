# feedback.py
import logging
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram import Dispatcher, F

logger = logging.getLogger(__name__)

# ---------- настройки ----------
with open("data.json", encoding="utf-8") as f:
    DATA = json.load(f)

HR_ADMIN_ID = DATA["admin_ids"][0]   # берём первый ID из admin_ids

# ---------- состояние ----------
class FeedbackStates(StatesGroup):
    typing = State()

# ---------- хэндлеры ----------
def register_feedback(dp: Dispatcher):
    """Подключаем хэндлеры к переданному Dispatcher"""

    @dp.callback_query(F.data == "leave_feedback")
    async def cb_leave_feedback(callback, state: FSMContext):
        await callback.message.edit_text(
            "✏️ Напишите ваш анонимный отзыв:\nчто нравится, что можно улучшить и т.д."
        )
        await state.set_state(FeedbackStates.typing)
        await callback.answer()

    @dp.message(FeedbackStates.typing)
    async def receive_feedback(msg: Message, state: FSMContext, bot):
        text = msg.text
        try:
            await bot.send_message(
                HR_ADMIN_ID,
                f"🆕 **Анонимный отзыв**\n\n{text}",
                parse_mode="Markdown"
            )
            answer_text = "✅ Спасибо! Ваш отзыв анонимно отправлен HR."
        except Exception as e:
            logger.warning("Не удалось отправить отзыв HR: %s", e)
            answer_text = "😔 К сожалению, сейчас не удалось отправить отзыв HR. Попробуйте позже."

        kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Главное меню", callback_data="main_menu")]]
        )
        await msg.answer(answer_text, reply_markup=kb)
        await state.clear()
