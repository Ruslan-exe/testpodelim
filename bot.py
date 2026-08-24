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
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo,
)
from sqlalchemy import or_, and_

from config import BOT_TOKEN, MINI_APP_URL
from database import get_db, init_db
from models import User, Friendship

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ---------- Локализация бота (ru / uz / en) ----------
TEXTS = {
    "ru": {
        "share_phone": "📱 Поделиться номером",
        "open_app": "🧾 Открыть Tolash",
        "welcome_back": "С возвращением! 👋\n\nОткрывай приложение — траты, долги и чеки уже ждут.",
        "welcome": (
            "Привет! Я — <b>Tolash</b> 🧾\n\n"
            "Один платит за всех — потом делим честно:\n"
            "  📸  сфоткай чек — разложу его по позициям сам\n"
            "  ✍️  или впиши траты вручную\n"
            "  👥  друзья отметят, кто что брал\n"
            "  💸  каждый увидит свою точную сумму\n\n"
            "Для начала подтверди номер телефона — по нему привязывается аккаунт."
        ),
        "own_contact": "Пришли, пожалуйста, свой собственный контакт кнопкой ниже.",
        "phone_ok": "Готово, номер привязан ✅\n\nЖми кнопку — и вперёд 👇",
        "ref_ok": "🤝 {name} добавлен(а) в твои друзья!\n\nТеперь вы можете делить счета вместе — открывай приложение 👇",
        "bill_invite": "🧾 Тебя пригласили разделить счёт!\n\nОткрывай приложение и отмечай свои позиции 👇",
    },
    "uz": {
        "share_phone": "📱 Raqamni ulashish",
        "open_app": "🧾 Tolashni ochish",
        "welcome_back": "Qaytganingiz bilan! 👋\n\nIlovani oching — xarajatlar, qarzlar va cheklar sizni kutmoqda.",
        "welcome": (
            "Salom! Men — <b>Tolash</b> 🧾\n\n"
            "Bittangiz hamma uchun to'laydi — keyin adolatli bo'lamiz:\n"
            "  📸  chekni suratga oling — o'zim ajratib beraman\n"
            "  ✍️  yoki xarajatlarni qo'lda kiriting\n"
            "  👥  do'stlar nima olganini belgilaydi\n"
            "  💸  har kim o'z aniq summasini ko'radi\n\n"
            "Boshlash uchun telefon raqamingizni tasdiqlang."
        ),
        "own_contact": "Iltimos, quyidagi tugma orqali o'z kontaktingizni yuboring.",
        "phone_ok": "Tayyor, raqam bog'landi ✅\n\nTugmani bosing 👇",
        "ref_ok": "🤝 {name} do'stlaringizga qo'shildi!\n\nEndi cheklarni birga bo'lishingiz mumkin — ilovani oching 👇",
        "bill_invite": "🧾 Sizni chekni bo'lishishga taklif qilishdi!\n\nIlovani oching va o'z pozitsiyalaringizni belgilang 👇",
    },
    "en": {
        "share_phone": "📱 Share phone number",
        "open_app": "🧾 Open Tolash",
        "welcome_back": "Welcome back! 👋\n\nOpen the app — your expenses, debts and receipts are waiting.",
        "welcome": (
            "Hi! I'm <b>Tolash</b> 🧾\n\n"
            "One person pays for everyone — then we split it fairly:\n"
            "  📸  snap the receipt — I'll parse every item\n"
            "  ✍️  or add expenses manually\n"
            "  👥  friends mark what they had\n"
            "  💸  everyone sees their exact share\n\n"
            "First, confirm your phone number to link your account."
        ),
        "own_contact": "Please share your own contact using the button below.",
        "phone_ok": "Done, phone linked ✅\n\nTap the button and go 👇",
        "ref_ok": "🤝 {name} is now your friend!\n\nYou can split bills together — open the app 👇",
        "bill_invite": "🧾 You've been invited to split a bill!\n\nOpen the app and mark your items 👇",
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


def _get_or_create_user(db, tg_user, lang: str) -> User:
    user = db.get(User, tg_user.id)
    if not user:
        user = User(
            id=tg_user.id, first_name=tg_user.first_name,
            username=tg_user.username, lang=lang,
        )
        db.add(user)
        db.commit()
    return user


def _ensure_friends_db(db, a: int, b: int):
    """Связь "друзья" (та же логика, что в main.py)."""
    if not a or not b or a == b or a < 0 or b < 0:
        return
    exists = db.query(Friendship).filter(
        or_(
            and_(Friendship.user_id == a, Friendship.friend_id == b),
            and_(Friendship.user_id == b, Friendship.friend_id == a),
        )
    ).first()
    if not exists:
        db.add(Friendship(user_id=a, friend_id=b))


@dp.message(CommandStart(deep_link=True))
async def on_start_deeplink(message: Message, command: CommandObject):
    """Deep-link: t.me/<бот>?start=ref_<id> (реферал) или ?start=<bill_id>
    (приглашение в счёт). Работает надёжно в любом Telegram, в отличие от
    ?startapp=, которому нужен настроенный Main Mini App."""
    payload = (command.args or "").strip()
    db = next(get_db())
    user = db.get(User, message.from_user.id)
    lang = _lang(message, user)
    user = _get_or_create_user(db, message.from_user, lang)

    if payload.startswith("ref_"):
        # Реферальная ссылка: сразу делаем обоих друзьями
        try:
            ref_id = int(payload[4:])
        except ValueError:
            ref_id = 0
        ref = db.get(User, ref_id) if ref_id > 0 else None
        if ref and ref.id != user.id:
            _ensure_friends_db(db, user.id, ref.id)
            if user.referred_by is None:
                user.referred_by = ref.id
            db.commit()
            await message.answer(
                TEXTS[lang]["ref_ok"].format(name=ref.first_name or "@" + (ref.username or "?")),
                reply_markup=_open_app_keyboard(lang),
            )
        else:
            await message.answer(TEXTS[lang]["welcome_back"], reply_markup=_open_app_keyboard(lang))
    elif payload.isdigit():
        # Приглашение в конкретный счёт
        await message.answer(
            TEXTS[lang]["bill_invite"],
            reply_markup=_open_app_keyboard(lang, int(payload)),
        )
    else:
        await message.answer(TEXTS[lang]["welcome_back"], reply_markup=_open_app_keyboard(lang))

    # Если номер ещё не привязан — сразу просим (регистрация нового друга)
    if not user.phone:
        await message.answer(
            TEXTS[lang]["welcome"],
            reply_markup=_contact_keyboard(lang),
            parse_mode="HTML",
        )


@dp.message(CommandStart())
async def on_start(message: Message):
    db = next(get_db())
    user = db.get(User, message.from_user.id)
    lang = _lang(message, user)
    user = _get_or_create_user(db, message.from_user, lang)

    if user.phone:
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
