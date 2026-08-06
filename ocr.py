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


def _shrink_image(raw_bytes: bytes, max_side: int = 1600, quality: int = 82) -> bytes:
    """Сжимает фото перед отправкой в API — экономит деньги на каждом чеке."""
    img = Image.open(io.BytesIO(raw_bytes))
    img = img.convert("RGB")
    w, h = img.size
    scale = min(1.0, max_side / max(w, h))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))
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

Правила:
- total — это итоговая сумма к оплате, как написано в чеке. Это самое важное поле,
  не ошибись в нём.
- Если в чеке нет отдельных полей налога/сервисного сбора — верни 0.
- Если qty не указано явно — считай 1.
- Если total_price позиции не совпадает с qty*unit_price (округления в самом чеке) —
  используй то, что реально напечатано в чеке, не пересчитывай.
- Если что-то не читается — сделай лучшее предположение, не выдумывай позиции,
  которых нет на фото."""


def parse_receipt(image_bytes: bytes) -> dict:
    """Отправляет фото чека в vision-модель и возвращает разобранную структуру.

    Возвращает dict с ключами currency/items/subtotal/tax/service_fee/total.
    Бросает ValueError, если модель вернула не-JSON (редко, но нужно уметь
    показать пользователю "не смогли распознать, попробуй фото почётче").
    """
    client = _get_client()
    small = _shrink_image(image_bytes)
    b64 = base64.b64encode(small).decode("utf-8")

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Распознай этот чек и верни JSON по описанной схеме."},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Модель вернула не-JSON, распознавание не удалось: {e}\n{raw}")

    data.setdefault("currency", DEFAULT_CURRENCY)
    data.setdefault("items", [])
    data.setdefault("subtotal", None)
    data.setdefault("tax", 0)
    data.setdefault("service_fee", 0)
    if "total" not in data or data["total"] is None:
        # подстраховка: если модель не дала total — считаем сами
        data["total"] = sum(float(i.get("total_price") or 0) for i in data["items"])

    return data
