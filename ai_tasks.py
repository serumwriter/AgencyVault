import os
import sqlite3
from datetime import datetime

DB_PATH = os.getenv("DATABASE_URL", "crm.db")

def get_db():
    return sqlite3.connect(DB_PATH)

def ensure_ai_tasks_table():
    db = get_db()
    c = db.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS ai_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id INTEGER,
        task_type TEXT NOT NULL,
        payload TEXT,
        run_at TEXT,
        status TEXT DEFAULT 'PENDING',
        result TEXT,
        created_at TEXT
    )
    """)
    db.commit()
    db.close()

def create_task(task_type, payload=None, lead_id=None, run_at=None):
    db = get_db()
    c = db.cursor()
    c.execute("""
        INSERT INTO ai_tasks (lead_id, task_type, payload, run_at, status, created_at)
        VALUES (?, ?, ?, ?, 'PENDING', ?)
    """, (
        lead_id,
        task_type,
        payload,
        run_at,
        datetime.utcnow().isoformat()
    ))
    db.commit()
    db.close()
