"""
FastAPI backend для Mini App "Поделим".

Эндпоинты:
  POST /api/bills                  -> создать счёт из фото (OCR)
  GET  /api/bills/{id}             -> текущее состояние счёта (позиции, кто что забрал)
  POST /api/bills/{id}/claims      -> пользователь отмечает "это моё" на позиции
  DELETE /api/bills/{id}/claims/{item_id} -> снять свою долю с позиции
  GET  /api/bills/{id}/split       -> итоговый расчёт "кто сколько должен"

Авторизация — через заголовок X-Telegram-Init-Data (это initData из
Telegram.WebApp), проверяется в telegram_auth.validate_init_data.
"""
import logging
from decimal import Decimal

from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from aiogram.types import Update

from database import get_db, init_db
from models import User, Bill, Item, Participant, ItemShare
from ocr import parse_receipt
from splitter import compute_split
from telegram_auth import validate_init_data
from config import USE_WEBHOOK, PUBLIC_BACKEND_URL, WEBHOOK_SECRET
from bot import bot as tg_bot, dp as tg_dp

logger = logging.getLogger("podelim")

app = FastAPI(title="Podelim API")

# CORS открыт широко, т.к. Mini App может быть захостен отдельно от API —
# для MVP это ок, для прода лучше сузить до конкретного домена.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup():
    init_db()
    if USE_WEBHOOK:
        if not PUBLIC_BACKEND_URL:
            raise RuntimeError(
                "USE_WEBHOOK=true, но PUBLIC_BACKEND_URL не задан в .env — "
                "без него бот не знает, куда Telegram должен слать обновления."
            )
        await tg_bot.set_webhook(f"{PUBLIC_BACKEND_URL}/webhook/{WEBHOOK_SECRET}")


@app.post(f"/webhook/{WEBHOOK_SECRET}")
async def telegram_webhook(request: Request):
    """Сюда Telegram присылает каждое сообщение боту, если USE_WEBHOOK=true.
    Секретный кусок URL (WEBHOOK_SECRET) — простая защита от того, чтобы
    кто-то посторонний слал сюда поддельные апдейты."""
    payload = await request.json()
    update = Update.model_validate(payload)
    await tg_dp.feed_update(tg_bot, update)
    return {"ok": True}


def get_current_user(
    db: Session = Depends(get_db),
    x_telegram_init_data: str = Header(default=""),
) -> User:
    try:
        tg_user = validate_init_data(x_telegram_init_data)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))

    user = db.get(User, tg_user["id"])
    if not user:
        user = User(
            id=tg_user["id"],
            first_name=tg_user.get("first_name"),
            username=tg_user.get("username"),
        )
        db.add(user)
        db.commit()
    return user


def _serialize_bill(bill: Bill) -> dict:
    return {
        "id": bill.id,
        "title": bill.title,
        "currency": bill.currency,
        "subtotal": bill.subtotal,
        "tax": bill.tax,
        "service_fee": bill.service_fee,
        "total": bill.total,
        "status": bill.status,
        "creator_id": bill.creator_id,
        "items": [
            {
                "id": item.id,
                "name": item.name,
                "qty": item.qty,
                "unit_price": item.unit_price,
                "total_price": item.total_price,
                "shares": [
                    {"user_id": s.user_id, "weight": s.weight}
                    for s in item.shares
                ],
            }
            for item in bill.items
        ],
        "participants": [
            {"user_id": p.user_id, "name": p.display_name}
            for p in bill.participants
        ],
    }


@app.post("/api/bills")
def create_bill(
    photo: UploadFile = File(...),
    title: str = Form(default="Счёт"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    image_bytes = photo.file.read()
    try:
        parsed = parse_receipt(image_bytes)
    except Exception:
        # Полный текст ошибки — только в серверный лог (для отладки).
        # Пользователю показываем короткое человеческое сообщение, без
        # технических деталей (класс исключения, стектрейс и т.п.).
        logger.exception("Не удалось распознать чек")
        raise HTTPException(
            status_code=422,
            detail="Не смогли распознать чек. Попробуй сфотографировать при хорошем свете, без бликов и так, чтобы весь чек попал в кадр.",
        )

    subtotal = parsed.get("subtotal")
    if subtotal is None:
        subtotal = sum(float(i.get("total_price") or 0) for i in parsed["items"])

    bill = Bill(
        creator_id=user.id,
        title=title,
        currency=parsed.get("currency", "UZS"),
        subtotal=subtotal,
        tax=parsed.get("tax") or 0,
        service_fee=parsed.get("service_fee") or 0,
        total=parsed.get("total") or subtotal,
        raw_ocr_json=str(parsed),
        status="splitting",
    )
    db.add(bill)
    db.flush()

    for i in parsed["items"]:
        db.add(Item(
            bill_id=bill.id,
            name=i.get("name", "Позиция"),
            qty=i.get("qty") or 1,
            unit_price=i.get("unit_price") or 0,
            total_price=i.get("total_price") or 0,
        ))

    db.add(Participant(bill_id=bill.id, user_id=user.id, display_name=user.first_name))
    db.commit()
    db.refresh(bill)
    return _serialize_bill(bill)


@app.get("/api/bills/{bill_id}")
def get_bill(bill_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")

    # Каждый, кто открыл счёт по ссылке, автоматически становится участником —
    # это MVP-упрощение, которое соответствует сценарию "все за одним столом".
    already = any(p.user_id == user.id for p in bill.participants)
    if not already:
        db.add(Participant(bill_id=bill.id, user_id=user.id, display_name=user.first_name))
        db.commit()
        db.refresh(bill)

    return _serialize_bill(bill)


@app.post("/api/bills/{bill_id}/claims")
def claim_item(
    bill_id: int,
    item_id: int = Form(...),
    weight: float = Form(default=1.0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = db.get(Item, item_id)
    if not item or item.bill_id != bill_id:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    existing = next((s for s in item.shares if s.user_id == user.id), None)
    if existing:
        existing.weight = weight
    else:
        db.add(ItemShare(item_id=item_id, user_id=user.id, weight=weight))
    db.commit()

    bill = db.get(Bill, bill_id)
    return _serialize_bill(bill)


@app.delete("/api/bills/{bill_id}/claims/{item_id}")
def unclaim_item(
    bill_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    item = db.get(Item, item_id)
    if not item or item.bill_id != bill_id:
        raise HTTPException(status_code=404, detail="Позиция не найдена")

    existing = next((s for s in item.shares if s.user_id == user.id), None)
    if existing:
        db.delete(existing)
        db.commit()

    bill = db.get(Bill, bill_id)
    return _serialize_bill(bill)


@app.get("/api/bills/{bill_id}/split")
def get_split(bill_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")

    items_with_shares = [
        (item.total_price, {s.user_id: s.weight for s in item.shares})
        for item in bill.items
    ]
    result = compute_split(bill, items_with_shares, bill.tax, bill.service_fee, bill.total)

    names = {p.user_id: (p.display_name or str(p.user_id)) for p in bill.participants}

    return {
        "currency": bill.currency,
        "total": bill.total,
        "fully_claimed": result["fully_claimed"],
        "unclaimed_amount": str(result["unclaimed_amount"]),
        "per_user": [
            {"user_id": uid, "name": names.get(uid, str(uid)), "amount": str(amount)}
            for uid, amount in sorted(result["per_user"].items(), key=lambda kv: -float(kv[1]))
        ],
    }
