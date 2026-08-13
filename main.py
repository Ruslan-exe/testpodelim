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
from decimal import Decimal
from datetime import datetime, timedelta
from collections import defaultdict

from pydantic import BaseModel
from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, Form, Request, Query
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
            {"user_id": p.user_id, "name": p.display_name, "confirmed": bool(p.confirmed)}
            for p in bill.participants
        ],
    }


def _reset_confirmations(bill: Bill):
    """Антифрод: любое изменение позиций счёта сбрасывает подтверждения
    у ВСЕХ участников — каждый должен перепроверить и подтвердить заново."""
    for p in bill.participants:
        p.confirmed = False


def _reset_my_confirmation(bill: Bill, user_id: int):
    """Изменил свой выбор — твоё подтверждение больше не действует."""
    for p in bill.participants:
        if p.user_id == user_id:
            p.confirmed = False


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
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Не удалось распознать чек: {e}")

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
    bill = db.get(Bill, bill_id)
    _reset_my_confirmation(bill, user.id)
    db.commit()
    db.refresh(bill)
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
    bill = db.get(Bill, bill_id)
    _reset_my_confirmation(bill, user.id)
    db.commit()
    db.refresh(bill)
    return _serialize_bill(bill)


@app.get("/api/bills/{bill_id}/split")
def get_split(bill_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")

    result = _bill_split(bill)

    names = {p.user_id: (p.display_name or str(p.user_id)) for p in bill.participants}
    confirmed = {p.user_id: bool(p.confirmed) for p in bill.participants}

    return {
        "currency": bill.currency,
        "total": bill.total,
        "fully_claimed": result["fully_claimed"],
        "unclaimed_amount": str(result["unclaimed_amount"]),
        "all_confirmed": all(confirmed.values()) if confirmed else False,
        "confirmed": [
            {"user_id": uid, "confirmed": c} for uid, c in confirmed.items()
        ],
        "per_user": [
            {"user_id": uid, "name": names.get(uid, str(uid)), "amount": str(amount)}
            for uid, amount in sorted(result["per_user"].items(), key=lambda kv: -float(kv[1]))
        ],
    }


# ============================================================
#  Новые эндпоинты: профиль, сводка трат, долги, ручные траты
# ============================================================

def _bill_split(bill: Bill) -> dict:
    """Пересчёт долей по счёту (без сериализации ответа).
    Неотмеченные позиции делятся поровну между всеми участниками."""
    items_with_shares = [
        (item.total_price, {s.user_id: s.weight for s in item.shares})
        for item in bill.items
    ]
    participant_ids = [p.user_id for p in bill.participants]
    return compute_split(
        bill, items_with_shares, bill.tax, bill.service_fee, bill.total,
        participant_ids=participant_ids,
    )


def _my_bills(db: Session, user_id: int) -> list:
    """Все счета, где пользователь — участник."""
    rows = db.query(Participant).filter(Participant.user_id == user_id).all()
    return [r.bill for r in rows if r.bill is not None]


PERIODS = {"day": 1, "week": 7, "month": 30, "all": None}


class ManualItem(BaseModel):
    name: str
    qty: float = 1.0
    unit_price: float = 0.0
    total_price: float = 0.0


class ManualBill(BaseModel):
    title: str = "Счёт"
    currency: str = "UZS"
    items: list[ManualItem] = []


class LangUpdate(BaseModel):
    lang: str  # ru | uz | en


@app.get("/api/me")
def get_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    bills = _my_bills(db, user.id)
    total_spent = Decimal("0")
    for b in bills:
        per_user = _bill_split(b)["per_user"]
        total_spent += per_user.get(user.id, Decimal("0"))
    return {
        "id": user.id,
        "first_name": user.first_name,
        "username": user.username,
        "phone": user.phone,
        "lang": user.lang or "ru",
        "bills_count": len(bills),
        "total_spent": str(total_spent),
    }


@app.post("/api/me/lang")
def set_lang(payload: LangUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if payload.lang not in ("ru", "uz", "en"):
        raise HTTPException(status_code=422, detail="lang must be ru|uz|en")
    user.lang = payload.lang
    db.commit()
    return {"ok": True, "lang": user.lang}


@app.get("/api/me/bills")
def my_bills(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    bills = sorted(_my_bills(db, user.id), key=lambda b: b.created_at or datetime.utcnow(), reverse=True)
    out = []
    for b in bills:
        per_user = _bill_split(b)["per_user"]
        out.append({
            "id": b.id,
            "title": b.title,
            "currency": b.currency,
            "total": b.total,
            "my_share": str(per_user.get(user.id, Decimal("0"))),
            "participants": len(b.participants),
            "status": b.status,
            "is_creator": b.creator_id == user.id,
            "created_at": (b.created_at or datetime.utcnow()).isoformat(),
        })
    return out


@app.get("/api/me/summary")
def my_summary(
    period: str = Query(default="month"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    days = PERIODS.get(period, 30)
    since = datetime.utcnow() - timedelta(days=days) if days else None

    bills = _my_bills(db, user.id)
    total = Decimal("0")
    count = 0
    by_bill = []
    for b in bills:
        if since and (b.created_at or datetime.utcnow()) < since:
            continue
        per_user = _bill_split(b)["per_user"]
        share = per_user.get(user.id, Decimal("0"))
        if share > 0:
            total += share
            count += 1
            by_bill.append({
                "id": b.id, "title": b.title, "amount": str(share),
                "currency": b.currency,
                "created_at": (b.created_at or datetime.utcnow()).isoformat(),
            })
    by_bill.sort(key=lambda x: x["created_at"], reverse=True)
    return {"period": period, "total": str(total), "bills_count": count, "bills": by_bill[:20]}


@app.get("/api/me/debts")
def my_debts(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Кто должен мне и кому должен я.

    Логика MVP: создатель счёта заплатил за всех → каждый участник
    должен создателю свою долю. Закрытые счета (status=closed) не считаются.
    """
    bills = _my_bills(db, user.id)
    owed_to_me = defaultdict(Decimal)   # user_id -> сумма
    i_owe = defaultdict(Decimal)        # creator_id -> сумма
    names = {}

    for b in bills:
        if b.status == "closed":
            continue
        per_user = _bill_split(b)["per_user"]
        for p in b.participants:
            names[p.user_id] = p.display_name or str(p.user_id)
        if b.creator_id == user.id:
            for uid, amount in per_user.items():
                if uid != user.id and amount > 0:
                    owed_to_me[uid] += amount
        else:
            my_share = per_user.get(user.id, Decimal("0"))
            if my_share > 0:
                i_owe[b.creator_id] += my_share

    return {
        "they_owe_me": [
            {"user_id": uid, "name": names.get(uid, str(uid)), "amount": str(amt)}
            for uid, amt in sorted(owed_to_me.items(), key=lambda kv: -kv[1])
        ],
        "i_owe": [
            {"user_id": uid, "name": names.get(uid, str(uid)), "amount": str(amt)}
            for uid, amt in sorted(i_owe.items(), key=lambda kv: -kv[1])
        ],
        "total_owed_to_me": str(sum(owed_to_me.values()) or Decimal("0")),
        "total_i_owe": str(sum(i_owe.values()) or Decimal("0")),
    }


@app.post("/api/bills/manual")
def create_manual_bill(
    payload: ManualBill,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Создать трату вручную, без фото: название + позиции."""
    items = payload.items
    for it in items:
        if not it.total_price:
            it.total_price = round(it.qty * it.unit_price, 2)
    subtotal = sum(i.total_price for i in items)

    bill = Bill(
        creator_id=user.id,
        title=payload.title or "Счёт",
        currency=payload.currency or "UZS",
        subtotal=subtotal,
        tax=0, service_fee=0,
        total=subtotal,
        status="splitting",
    )
    db.add(bill)
    db.flush()
    for it in items:
        db.add(Item(
            bill_id=bill.id, name=it.name, qty=it.qty,
            unit_price=it.unit_price or (it.total_price / it.qty if it.qty else it.total_price),
            total_price=it.total_price,
        ))
    db.add(Participant(bill_id=bill.id, user_id=user.id, display_name=user.first_name))
    db.commit()
    db.refresh(bill)
    return _serialize_bill(bill)


@app.post("/api/bills/{bill_id}/items")
def add_item(
    bill_id: int,
    payload: ManualItem,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Добавить позицию в существующий счёт вручную."""
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    total_price = payload.total_price or round(payload.qty * payload.unit_price, 2)
    db.add(Item(
        bill_id=bill.id, name=payload.name, qty=payload.qty,
        unit_price=payload.unit_price or (total_price / payload.qty if payload.qty else total_price),
        total_price=total_price,
    ))
    bill.subtotal = (bill.subtotal or 0) + total_price
    bill.total = (bill.total or 0) + total_price
    _reset_confirmations(bill)
    db.commit()
    db.refresh(bill)
    return _serialize_bill(bill)


@app.patch("/api/bills/{bill_id}/items/{item_id}")
def edit_item(
    bill_id: int,
    item_id: int,
    payload: ManualItem,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Изменить позицию (название/цену/кол-во). Только создатель счёта.
    Сбрасывает подтверждения всех участников — суммы изменились."""
    bill = db.get(Bill, bill_id)
    item = db.get(Item, item_id)
    if not bill or not item or item.bill_id != bill_id:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    if bill.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Изменять позиции может только создатель счёта")

    new_total = payload.total_price or round(payload.qty * payload.unit_price, 2)
    bill.subtotal = max(0, (bill.subtotal or 0) - item.total_price + new_total)
    bill.total = max(0, (bill.total or 0) - item.total_price + new_total)

    item.name = payload.name or item.name
    item.qty = payload.qty or 1
    item.unit_price = payload.unit_price or (new_total / item.qty if item.qty else new_total)
    item.total_price = new_total

    _reset_confirmations(bill)
    db.commit()
    db.refresh(bill)
    return _serialize_bill(bill)


class BillUpdate(BaseModel):
    title: str


@app.patch("/api/bills/{bill_id}")
def edit_bill(
    bill_id: int,
    payload: BillUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Переименовать счёт. Только создатель."""
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    if bill.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Переименовать счёт может только создатель")
    bill.title = payload.title.strip() or bill.title
    db.commit()
    db.refresh(bill)
    return _serialize_bill(bill)


@app.post("/api/bills/{bill_id}/confirm")
def confirm_choice(bill_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Участник подтверждает свой выбор позиций (антифрод-фиксация)."""
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    for p in bill.participants:
        if p.user_id == user.id:
            p.confirmed = True
            db.commit()
            db.refresh(bill)
            return _serialize_bill(bill)
    raise HTTPException(status_code=403, detail="Вы не участник этого счёта")


@app.delete("/api/bills/{bill_id}/confirm")
def unconfirm_choice(bill_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Снять своё подтверждение, чтобы изменить выбор."""
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    for p in bill.participants:
        if p.user_id == user.id:
            p.confirmed = False
            db.commit()
            db.refresh(bill)
            return _serialize_bill(bill)
    raise HTTPException(status_code=403, detail="Вы не участник этого счёта")


@app.delete("/api/bills/{bill_id}/items/{item_id}")
def delete_item(
    bill_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Удалить позицию (только создатель счёта)."""
    bill = db.get(Bill, bill_id)
    item = db.get(Item, item_id)
    if not bill or not item or item.bill_id != bill_id:
        raise HTTPException(status_code=404, detail="Позиция не найдена")
    if bill.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Удалять позиции может только создатель счёта")
    bill.subtotal = max(0, (bill.subtotal or 0) - item.total_price)
    bill.total = max(0, (bill.total or 0) - item.total_price)
    db.delete(item)
    _reset_confirmations(bill)
    db.commit()
    db.refresh(bill)
    return _serialize_bill(bill)


@app.post("/api/bills/{bill_id}/close")
def close_bill(bill_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Закрыть счёт — все рассчитались, долги по нему больше не показываем."""
    bill = db.get(Bill, bill_id)
    if not bill:
        raise HTTPException(status_code=404, detail="Счёт не найден")
    if bill.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Закрыть счёт может только его создатель")
    bill.status = "closed"
    db.commit()
    return {"ok": True, "status": bill.status}
