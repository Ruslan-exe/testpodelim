"""
Распознавание чека через vision-модель (LLM с поддержкой изображений).

Почему так, а не классический OCR: чеки в Узбекистане — смесь узбекского,
русского, разного качества печати. LLM с vision одновременно читает текст
И понимает структуру ("это позиция", "это налог", "это итог") на любом
языке без отдельного обучения. Для MVP это быстрее и дешевле, чем городить
конвейер OCR + парсер строк.

ВАЖНО про стоимость: каждый вызов — это реальные деньги с твоей карты
(OpenAI биллинг). Изображение перед отправкой ужимается, чтобы не палить
бюджет на фото 12 Мп с телефона.
"""
import base64
import io
import json

from openai import OpenAI
from PIL import Image

from config import OPENAI_API_KEY, OPENAI_BASE_URL, VISION_MODEL, DEFAULT_CURRENCY

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        if not OPENAI_API_KEY:
            raise RuntimeError(
                "OPENAI_API_KEY не задан в .env — без него распознавание чеков работать не будет."
            )
        _client = OpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
    return _client


def _shrink_image(raw_bytes: bytes, max_side: int = 2200, quality: int = 92) -> bytes:
    """Готовит фото чека для vision-модели.

    Чеки — длинные и узкие, с мелким термошрифтом. Старые настройки
    (1600px, quality 82) «убивали» мелкий текст — модель начинала
    угадывать цифры. Теперь: больше разрешение, авто-контраст и
    лёгкая резкость — это даёт основной прирост точности бесплатно.
    """
    from PIL import ImageOps, ImageEnhance

    img = Image.open(io.BytesIO(raw_bytes))
    img = ImageOps.exif_transpose(img)  # уважаем ориентацию с телефона
    img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    elif longest < 1400:
        # Маленькие фото/скриншоты (чек в треть кадра шириной ~300px) —
        # УВЕЛИЧИВАЕМ: vision-модель выделяет больше "внимания" крупным
        # изображениям, и мелкие цифры перестают угадываться.
        scale = 1400 / longest
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    img = ImageOps.autocontrast(img, cutoff=1)          # вытянуть блёклую термопечать
    img = ImageEnhance.Sharpness(img).enhance(1.4)      # подчеркнуть мелкие цифры

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


SYSTEM_PROMPT = """Ты распознаёшь фото чека (Узбекистан, Россия, СНГ — язык может быть
узбекский, русский или смесь). Верни СТРОГО валидный JSON без markdown-обёртки, без
комментариев, по такой схеме:

{
  "currency": "UZS | RUB | USD | ...",
  "items": [
    {"name": "строка как в чеке", "qty": число, "unit_price": число, "total_price": число}
  ],
  "subtotal": число или null,
  "tax": число или null,
  "service_fee": число или null,
  "total": число
}

Особенности фото (ВАЖНО):
- Фото может быть СКРИНШОТОМ переписки/галереи: игнорируй интерфейс телефона
  (часы, батарею, кнопки, подписи чата) — читай только сам бумажный чек.
- Чек может быть ПОВЁРНУТ на 90°/180°, наклонён, смят или изогнут — мысленно
  выпрями и поверни его, читай все строки.
- Чек может занимать малую часть кадра — сосредоточься только на нём.
- В ресторанных чеках Узбекистана часто ДВЕ колонки цифр: "Кол-во" и "Сумма".
  Строка "Мохито 1л   10   600 000" значит qty=10 и total_price=600000 —
  НЕ склеивай колонки в одно число 10600000! Сначала пойми структуру колонок
  по заголовку таблицы, потом читай строки.
- Строка "обслуживание 15%" — это service_fee, не позиция.

Правила:
- total — это итоговая сумма к оплате, как написано в чеке. Это самое важное поле,
  не ошибись в нём.
- ПРОВЕРЬ СЕБЯ перед ответом: сумма total_price всех позиций + tax + service_fee
  должна сходиться с total (допустимо расхождение на округления). Если не сходится —
  перечитай чек ещё раз: скорее всего ты пропустил позицию или перепутал цифру.
- Читай ЦЕНЫ ВНИМАТЕЛЬНО по разрядам: в UZS суммы крупные (десятки/сотни тысяч),
  разделители тысяч (пробелы, точки, запятые) — не десятичная часть.
  "65 000" и "65.000" — это 65000, а не 65.
- Если qty не указано явно — считай 1.
- Если total_price позиции не совпадает с qty*unit_price (округления в самом чеке) —
  используй то, что реально напечатано в чеке, не пересчитывай.
- Названия позиций пиши как в чеке (узбекский/русский), не переводи.
- Скидки: если после позиции идёт строка скидки — вычти её из total_price позиции.
- Не включай в items строки "Итого", "Наличные", "Сдача", "НДС" — это не позиции.
- Если что-то не читается — сделай лучшее предположение, не выдумывай позиции,
  которых нет на фото."""


def _call_vision(b64: str, extra_note: str = "") -> dict:
    client = _get_client()
    user_text = "Распознай этот чек и верни JSON по описанной схеме."
    if extra_note:
        user_text += "\n\n" + extra_note
    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"}},
                ],
            },
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    raw = response.choices[0].message.content
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Модель вернула не-JSON, распознавание не удалось: {e}\n{raw}")


def _sums_ok(data: dict, tolerance: float = 0.02) -> bool:
    """Арифметическая проверка: позиции + налог + сервис ≈ итог чека."""
    try:
        items_sum = sum(float(i.get("total_price") or 0) for i in data.get("items", []))
        extra = float(data.get("tax") or 0) + float(data.get("service_fee") or 0)
        total = float(data.get("total") or 0)
        if total <= 0 or not data.get("items"):
            return False
        return abs(items_sum + extra - total) <= max(total * tolerance, 1.0)
    except (TypeError, ValueError):
        return False


def parse_receipt(image_bytes: bytes) -> dict:
    """Отправляет фото чека в vision-модель и возвращает разобранную структуру.

    Двухпроходная схема: если после первого прохода сумма позиций не сходится
    с итогом чека — модель получает второй шанс с прямым указанием на ошибку.
    Это дёшево (второй вызов только при расхождении) и заметно поднимает точность.
    """
    small = _shrink_image(image_bytes)
    b64 = base64.b64encode(small).decode("utf-8")

    data = _call_vision(b64)

    if not _sums_ok(data):
        try:
            items_sum = sum(float(i.get("total_price") or 0) for i in data.get("items", []))
            retry = _call_vision(
                b64,
                "ВНИМАНИЕ: в прошлый раз сумма распознанных позиций "
                f"({items_sum:.0f}) не сошлась с итогом чека ({data.get('total')}). "
                "Перечитай чек заново, особенно цены и пропущенные строки, и верни исправленный JSON.",
            )
            if _sums_ok(retry):
                data = retry
        except Exception:
            pass  # второй проход — бонус; при сбое остаёмся с первым результатом

    data.setdefault("currency", DEFAULT_CURRENCY)
    data.setdefault("items", [])
    data.setdefault("subtotal", None)
    data.setdefault("tax", 0)
    data.setdefault("service_fee", 0)
    if "total" not in data or data["total"] is None:
        # подстраховка: если модель не дала total — считаем сами
        data["total"] = sum(float(i.get("total_price") or 0) for i in data["items"])

    return data
