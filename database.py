from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models import Base

engine = create_engine(
    DATABASE_URL,
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
    add_column_if_missing("participants", "confirmed", "confirmed BOOLEAN DEFAULT 0")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
