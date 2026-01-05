from datetime import datetime
from sqlalchemy import create_engine, text
import os

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def ensure_ai_tasks_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_tasks (
                id SERIAL PRIMARY KEY,
                task_type TEXT NOT NULL,
                lead_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'NEW',
                due_at TIMESTAMP,
                notes TEXT,
                attempt INTEGER DEFAULT 1,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """))

def create_task(task_type, lead_id, notes=None, due_at=None):
    ensure_ai_tasks_table()
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO ai_tasks (task_type, lead_id, notes, due_at)
            VALUES (:task_type, :lead_id, :notes, :due_at)
        """), {
            "task_type": task_type,
            "lead_id": lead_id,
            "notes": notes,
            "due_at": due_at
        })
