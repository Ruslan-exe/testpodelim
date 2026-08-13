"""
Модель данных.

Инвариант продукта («общая сумма всегда должна сходиться») обеспечивается
не здесь, а в splitter.py — но именно здесь заложена структура, которая
это делает возможным: чек -> позиции -> доли участников на КАЖДУЮ позицию
(а не на чек в целом), плюс отдельно налог/сервисный сбор, которые
раскладываются пропорционально.
"""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Float, DateTime, ForeignKey, Boolean
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)  # это же telegram_id
    phone = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    username = Column(String, nullable=True)
    lang = Column(String, default="ru")  # ru | uz | en — язык интерфейса
    created_at = Column(DateTime, default=datetime.utcnow)


class Bill(Base):
    __tablename__ = "bills"

    id = Column(Integer, primary_key=True)
    creator_id = Column(Integer, ForeignKey("users.id"))
    title = Column(String, default="Счёт")
    currency = Column(String, default="UZS")

    subtotal = Column(Float, default=0.0)   # сумма позиций без налога/сервиса
    tax = Column(Float, default=0.0)
    service_fee = Column(Float, default=0.0)
    total = Column(Float, default=0.0)      # итог по чеку (то, что реально нужно закрыть)

    raw_ocr_json = Column(String, nullable=True)  # сырой ответ vision-модели, для отладки качества
    status = Column(String, default="draft")  # draft -> splitting -> closed
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("Item", back_populates="bill", cascade="all, delete-orphan")
    participants = relationship("Participant", back_populates="bill", cascade="all, delete-orphan")


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    bill_id = Column(Integer, ForeignKey("bills.id"))
    name = Column(String)
    qty = Column(Float, default=1.0)
    unit_price = Column(Float, default=0.0)
    total_price = Column(Float, default=0.0)  # qty * unit_price (или как в чеке, если не сходится)

    bill = relationship("Bill", back_populates="items")
    shares = relationship("ItemShare", back_populates="item", cascade="all, delete-orphan")


class Participant(Base):
    """Кто из пользователей Telegram участвует в этом конкретном счёте."""
    __tablename__ = "participants"

    id = Column(Integer, primary_key=True)
    bill_id = Column(Integer, ForeignKey("bills.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    display_name = Column(String, nullable=True)
    # Антифрод: участник подтвердил свой выбор позиций. Любое изменение
    # позиций счёта (создателем) сбрасывает подтверждения у ВСЕХ.
    confirmed = Column(Boolean, default=False)

    bill = relationship("Bill", back_populates="participants")


class ItemShare(Base):
    """
    Доля конкретного участника в конкретной позиции чека.
    weight позволяет делить одну позицию не только поровну, но и, например,
    2 к 1 (кто-то съел половину блюда, а не долю 1/N) — задел под 'умный' делёж.
    """
    __tablename__ = "item_shares"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    weight = Column(Float, default=1.0)

    item = relationship("Item", back_populates="shares")
