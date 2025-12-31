# database.py (ROOT)
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

def _clean_database_url(url: str) -> str:
    return (url or "").strip()

DATABASE_URL = _clean_database_url(os.getenv("DATABASE_URL"))

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing. Set it in Render environment variables.")

# Render Postgres often uses postgres:// but SQLAlchemy prefers postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

class Base(DeclarativeBase):
    pass

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
