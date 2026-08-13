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


# ---------- Локализация бота (ru / uz / en) ----------
TEXTS = {
    "ru": {
        "share_phone": "📱 Поделиться номером",
        "open_app": "🧾 Открыть Поделим",
        "welcome_back": "С возвращением! 👋\n\nОткрывай приложение — траты, долги и чеки уже ждут.",
        "welcome": (
            "Привет! Я — <b>Поделим</b> 🧾\n\n"
            "Один платит за всех — потом делим честно:\n"
            "  📸  сфоткай чек — разложу его по позициям сам\n"
            "  ✍️  или впиши траты вручную\n"
            "  👥  друзья отметят, кто что брал\n"
            "  💸  каждый увидит свою точную сумму\n\n"
            "Для начала подтверди номер телефона — по нему привязывается аккаунт."
        ),
        "own_contact": "Пришли, пожалуйста, свой собственный контакт кнопкой ниже.",
        "phone_ok": "Готово, номер привязан ✅\n\nЖми кнопку — и вперёд 👇",
    },
    "uz": {
        "share_phone": "📱 Raqamni ulashish",
        "open_app": "🧾 Podelimni ochish",
        "welcome_back": "Qaytganingiz bilan! 👋\n\nIlovani oching — xarajatlar, qarzlar va cheklar sizni kutmoqda.",
        "welcome": (
            "Salom! Men — <b>Podelim</b> 🧾\n\n"
            "Bittangiz hamma uchun to'laydi — keyin adolatli bo'lamiz:\n"
            "  📸  chekni suratga oling — o'zim ajratib beraman\n"
            "  ✍️  yoki xarajatlarni qo'lda kiriting\n"
            "  👥  do'stlar nima olganini belgilaydi\n"
            "  💸  har kim o'z aniq summasini ko'radi\n\n"
            "Boshlash uchun telefon raqamingizni tasdiqlang."
        ),
        "own_contact": "Iltimos, quyidagi tugma orqali o'z kontaktingizni yuboring.",
        "phone_ok": "Tayyor, raqam bog'landi ✅\n\nTugmani bosing 👇",
    },
    "en": {
        "share_phone": "📱 Share phone number",
        "open_app": "🧾 Open Podelim",
        "welcome_back": "Welcome back! 👋\n\nOpen the app — your expenses, debts and receipts are waiting.",
        "welcome": (
            "Hi! I'm <b>Podelim</b> 🧾\n\n"
            "One person pays for everyone — then we split it fairly:\n"
            "  📸  snap the receipt — I'll parse every item\n"
            "  ✍️  or add expenses manually\n"
            "  👥  friends mark what they had\n"
            "  💸  everyone sees their exact share\n\n"
            "First, confirm your phone number to link your account."
        ),
        "own_contact": "Please share your own contact using the button below.",
        "phone_ok": "Done, phone linked ✅\n\nTap the button and go 👇",
    },
}


def _lang(message: Message, user: User | None = None) -> str:
    """Язык: сохранённый в профиле → язык клиента Telegram → русский."""
    if user is not None and getattr(user, "lang", None) in TEXTS:
        return user.lang
    code = (message.from_user.language_code or "ru").lower()
    if code.startswith("uz"):
        return "uz"
    if code.startswith("en"):
        return "en"
    return "ru"


def _contact_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=TEXTS[lang]["share_phone"], request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _open_app_keyboard(lang: str, bill_id: int | None = None) -> InlineKeyboardMarkup:
    # startapp=<bill_id> — так Mini App узнаёт, какой именно счёт открывать
    # (deep link для приглашения остальных участников в этот же счёт).
    url = MINI_APP_URL if bill_id is None else f"{MINI_APP_URL}?startapp={bill_id}"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=TEXTS[lang]["open_app"], web_app=WebAppInfo(url=url))
    ]])


@dp.message(CommandStart())
async def on_start(message: Message):
    db = next(get_db())
    user = db.get(User, message.from_user.id)
    lang = _lang(message, user)

    if user and user.phone:
        await message.answer(
            TEXTS[lang]["welcome_back"],
            reply_markup=_open_app_keyboard(lang),
        )
        return

    await message.answer(
        TEXTS[lang]["welcome"],
        reply_markup=_contact_keyboard(lang),
        parse_mode="HTML",
    )


@dp.message(F.contact)
async def on_contact(message: Message):
    db = next(get_db())
    user = db.get(User, message.from_user.id)
    lang = _lang(message, user)

    if message.contact.user_id != message.from_user.id:
        await message.answer(TEXTS[lang]["own_contact"])
        return

    if not user:
        user = User(id=message.from_user.id)
        db.add(user)

    user.phone = message.contact.phone_number
    user.first_name = message.from_user.first_name
    user.username = message.from_user.username
    user.lang = lang
    db.commit()

    await message.answer(
        TEXTS[lang]["phone_ok"],
        reply_markup=_open_app_keyboard(lang),
    )


async def main():
    """Только для локальной разработки. На проде main.py сам ставит вебхук."""
    init_db()
    await bot.delete_webhook(drop_pending_updates=True)  # иначе polling конфликтует с вебхуком
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
