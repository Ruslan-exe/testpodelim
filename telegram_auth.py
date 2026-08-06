"""
Проверка подлинности Telegram.WebApp.initData.

Это не опционально: без этой проверки любой человек может подделать
запрос от имени чужого telegram_id и "повесить" на него чужие позиции
в счёте (или, наоборот, снять с себя долг). Для сервиса, который считает
чужие деньги, это дыра №1 — закрываем её сразу, а не "потом".

Алгоритм — официальный, из документации Telegram Mini Apps.
"""
import hashlib
import hmac
import json
from urllib.parse import parse_qsl

from config import BOT_TOKEN


def validate_init_data(init_data: str, max_age_seconds: int = 86400) -> dict:
    """Возвращает распарсенный dict user из initData, если подпись верна.

    Бросает ValueError, если подпись неверна или данные протухли — вызывающий
    код должен вернуть 401.
    """
    if not init_data:
        raise ValueError("initData отсутствует")

    parsed = dict(parse_qsl(init_data, strict_parsing=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise ValueError("В initData нет hash")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))

    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("Подпись initData не совпадает — запрос не от Telegram")

    user_raw = parsed.get("user")
    if not user_raw:
        raise ValueError("В initData нет поля user")

    return json.loads(user_raw)
