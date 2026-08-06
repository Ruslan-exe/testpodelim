"""
Telegram-бот на aiogram 3.x.

Задачи бота минимальны и осознанно: он не считает деньги (это делает
Mini App через backend API), а только:
  1. регистрирует пользователя по номеру телефона (request_contact),
  2. открывает Mini App кнопкой.

Два режима запуска:
  - Локально для разработки: `python bot.py` — обычный long-polling,
    отдельный процесс, не нужен публичный HTTPS-адрес.
  - На проде (бесплатный хостинг с одним процессом, см. README): бот
    подключается через вебхук ПРЯМО ВНУТРИ main.py (см. импорт `bot`, `dp`
    оттуда) — отдельно запускать этот файл не нужно, всё поднимается
    вместе с `uvicorn main:app`.
"""
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)

from config import BOT_TOKEN, MINI_APP_URL
from database import get_db, init_db
from models import User

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def _contact_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _open_app_keyboard(bill_id: int | None = None) -> InlineKeyboardMarkup:
    # startapp=<bill_id> — так Mini App узнаёт, какой именно счёт открывать
    # (deep link для приглашения остальных участников в этот же счёт).
    url = MINI_APP_URL if bill_id is None else f"{MINI_APP_URL}?startapp={bill_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🧾 Открыть Поделим", web_app=WebAppInfo(url=url))
    ]])


@dp.message(CommandStart())
async def on_start(message: Message):
    db = next(get_db())
    user = db.get(User, message.from_user.id)

    if user and user.phone:
        await message.answer(
            "С возвращением! Открывай приложение и загружай фото чека.",
            reply_markup=_open_app_keyboard(),
        )
        return

    await message.answer(
        "Привет! Я — Поделим 🧾\n\n"
        "Сфоткал чек — я разложу его на позиции, а компания сама разберёт, "
        "кто что ел, и сумма сойдётся до тийина.\n\n"
        "Для начала подтверди номер телефона — по нему привязывается аккаунт.",
        reply_markup=_contact_keyboard(),
    )


@dp.message(F.contact)
async def on_contact(message: Message):
    if message.contact.user_id != message.from_user.id:
        await message.answer("Пришли, пожалуйста, свой собственный контакт кнопкой ниже.")
        return

    db = next(get_db())
    user = db.get(User, message.from_user.id)
    if not user:
        user = User(id=message.from_user.id)
        db.add(user)

    user.phone = message.contact.phone_number
    user.first_name = message.from_user.first_name
    user.username = message.from_user.username
    db.commit()

    await message.answer(
        "Готово, номер привязан ✅\n\nТеперь жми кнопку и загружай фото чека.",
        reply_markup=_open_app_keyboard(),
    )


async def main():
    """Только для локальной разработки. На проде main.py сам ставит вебхук."""
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)  # иначе polling конфликтует с вебхуком
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
