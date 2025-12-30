import os
from datetime import datetime, timezone
from sqlalchemy import create_engine, text

# Uses the SAME Postgres as the app
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# ------------------------------
# TABLE SETUP
# ------------------------------
def ensure_ai_tasks_table():
    with engine.begin() as conn:
        conn.execute(text("""
        CREATE TABLE IF NOT EXISTS ai_tasks (
            id SERIAL PRIMARY KEY,
            task_type TEXT NOT NULL,
            lead_id INTEGER NULL,
            payload TEXT NULL,
            status TEXT NOT NULL DEFAULT 'NEW',
            run_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW()
        );
        """))

# ------------------------------
# TASK CREATION
# ------------------------------
def create_task(
    task_type: str,
    lead_id: int | None = None,
    payload: str | None = None,
    run_at: datetime | None = None
):
    ensure_ai_tasks_table()
    with engine.begin() as conn:
        conn.execute(
            text("""
            INSERT INTO ai_tasks
            (task_type, lead_id, payload, status, run_at, created_at, updated_at)
            VALUES
            (:task_type, :lead_id, :payload, 'NEW', :run_at, :now, :now)
            """),
            {
                "task_type": task_type,
                "lead_id": lead_id,
                "payload": payload,
                "run_at": run_at,
                "now": datetime.now(timezone.utc),
            }
        )

# ------------------------------
# TASK QUERIES
# ------------------------------
def fetch_ready_tasks(limit: int = 10):
    ensure_ai_tasks_table()
    with engine.begin() as conn:
        rows = conn.execute(
            text("""
            SELECT *
            FROM ai_tasks
            WHERE status = 'NEW'
              AND (run_at IS NULL OR run_at <= NOW())
            ORDER BY created_at
            LIMIT :limit
            """),
            {"limit": limit}
        ).mappings().all()
    return rows

def mark_task_status(task_id: int, status: str):
    with engine.begin() as conn:
        conn.execute(
            text("""
            UPDATE ai_tasks
            SET status = :status,
                updated_at = NOW()
            WHERE id = :id
            """),
            {"id": task_id, "status": status}
        )
