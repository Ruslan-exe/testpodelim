from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models import Base

engine = create_engine(
    DATABASE_URL,
    # Держим небольшой пул живых соединений и проверяем их перед использованием:
    # без этого после простоя первый запрос к облачному Postgres падает/тормозит.
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    pool_recycle=1800,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    """Мини-миграция: create_all не добавляет новые колонки в уже
    существующие таблицы, поэтому добавляем их вручную, если их нет."""
    from sqlalchemy import text, inspect

    inspector = inspect(engine)
    tables = inspector.get_table_names()

    def add_column_if_missing(table: str, column: str, ddl: str):
        if table not in tables:
            return
        cols = {c["name"] for c in inspector.get_columns(table)}
        if column not in cols:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))

    add_column_if_missing("users", "lang", "lang VARCHAR DEFAULT 'ru'")
    add_column_if_missing("users", "referred_by", "referred_by INTEGER")
    add_column_if_missing("participants", "confirmed", "confirmed BOOLEAN DEFAULT 0")

    # Индексы: create_all не добавляет их в уже существующие таблицы,
    # поэтому досоздаём недостающие (безопасно повторять при каждом старте).
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        existing = {ix["name"] for ix in inspector.get_indexes(table.name)}
        for ix in table.indexes:
            if ix.name not in existing:
                ix.create(bind=engine, checkfirst=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
