import os
from datetime import datetime
from sqlalchemy import create_engine, text

# Uses the SAME Postgres as the app
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def ensure_ai_tasks_table():
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS ai_tasks (
            id SERIAL PRIMARY KEY,
            task_type TEXT NOT NULL,
            lead_id INTEGER NULL,
            payload TEXT NULL,
            status TEXT NOT NULL DEFAULT 'NEW',
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """))

def create_task(task_type: str, lead_id: int | None = None, payload: str | None = None):
    ensure_ai_tasks_table()
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO ai_tasks (task_type, lead_id, payload, status, created_at)
            VALUES (:task_type, :lead_id, :payload, 'NEW', :created_at)
            """),
            {
                "task_type": task_type,
                "lead_id": lead_id,
                "payload": payload,
                "created_at": datetime.utcnow(),
            }
        )
