
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# If you later deploy to the cloud with PostgreSQL, set DATABASE_URL there.
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Example for Postgres in the cloud:
    # postgres://user:password@host:port/dbname
    engine = create_engine(DATABASE_URL)
else:
    # Local dev: simple SQLite file
    engine = create_engine(
        "sqlite:///./crm.db",
        connect_args={"check_same_thread": False}
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
