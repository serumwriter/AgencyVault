print(">>> LOADED agencyvault_app/main.py <<<")

# ==============================
# FLAGS
# ==============================
TWILIO_ENABLED = False

# ==============================
# IMPORTS
# ==============================
import os
import re
import csv
from datetime import datetime

from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base

from ai_tasks import create_task
from .ai_employee import run_ai_engine
from ai_tasks import fetch_ready_tasks, mark_task_status

# ==============================
# DATABASE
# ==============================
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

DATABASE_URL = DATABASE_URL.replace(
    "postgresql://", "postgresql+psycopg://"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# ==============================
# MODEL
# ==============================
class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    full_name = Column(String(255))
    phone = Column(String(50), index=True)
    email = Column(String(255))

    # AI fields
    state = Column(String(30), default="NEW")
    ai_priority = Column(Integer, default=0)
    ai_next_action = Column(String(50))
    ai_reason = Column(Text)
    
    call_attempts = Column(Integer, default=0)
    last_call_attempt_at = Column(DateTime)

    ai_last_action_at = Column(DateTime)
    ai_next_action_at = Column(DateTime)
    appointment_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)
from sqlalchemy import text

with engine.begin() as conn:
    conn.execute(text("""
        ALTER TABLE leads
        ADD COLUMN IF NOT EXISTS call_attempts INTEGER DEFAULT 0,
        ADD COLUMN IF NOT EXISTS last_call_attempt_at TIMESTAMP;
    """))

# ==============================
# APP
# ==============================
app = FastAPI(title="AgencyVault")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.add_middleware(SessionMiddleware, secret_key="CHANGE_ME")

# ==============================
# HELPERS
# ==============================
def normalize_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return d

def looks_like_phone(s):
    return len(re.sub(r"\D", "", s or "")) in (10, 11)

def looks_like_email(s):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", s or ""))

def looks_like_name(s):
    if not s:
        return False

    banned_words = {
        "lead", "gold", "silver", "bronze",
        "tier", "status", "priority",
        "hot", "warm", "cold", "prospect"
    }

    parts = s.strip().split()
    if not (2 <= len(parts) <= 3):
        return False

    for p in parts:
        if not p.isalpha():
            return False
        if not p[0].isupper():
            return False
        if p.lower() in banned_words:
            return False

    return True

# ==============================
# PWA ROUTES
# ==============================
@app.get("/manifest.json")
def manifest():
    return FileResponse(
        "app/static/manifest.json",
        media_type="application/manifest+json"
    )

@app.get("/sw.js")
def service_worker():
    return FileResponse(
        "app/static/sw.js",
        media_type="application/javascript"
    )

# ==============================
# SYSTEM ROUTES
# ==============================
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/ai/run")
def run_ai():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead, plan_only=True)

    created = 0
    for action in actions:
        create_task(
            task_type=action["type"],
            lead_id=action.get("lead_id"),
            payload=str(action)
        )
        created += 1

    db.close()
    return {"ok": True, "planned_tasks": created}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")
@app.get("/tasks")
def view_tasks():
    db = SessionLocal()

    # Pull actionable tasks only
    rows = db.execute("""
        SELECT
            t.id,
            t.task_type,
            t.created_at,
            l.full_name,
            l.phone,
            l.ai_priority
        FROM ai_tasks t
        JOIN leads l ON l.id = t.lead_id
        WHERE t.status = 'NEW'
          AND t.task_type IN ('CALL', 'WAIT')
        ORDER BY
            CASE t.task_type
                WHEN 'CALL' THEN 1
                WHEN 'WAIT' THEN 2
                ELSE 3
            END,
            l.ai_priority DESC,
            t.created_at
        LIMIT 50
    """).fetchall()

    db.close()

    cards = ""

    for r in rows:
        icon = "📞" if r.task_type == "CALL" else "⏳"
        when = r.created_at.strftime("%Y-%m-%d %H:%M UTC")

        cards += (
            "<div class='card'>"
            f"<h3>{icon} {r.task_type}</h3>"
            f"<b>{r.full_name}</b><br>"
            f"{r.phone}<br>"
            f"<small>Priority: {r.ai_priority}</small><br>"
            f"<small>Run at: {when}</small>"
            "</div>"
        )

    if not cards:
        cards = "<div class='card'>No actionable tasks</div>"

    return HTMLResponse(
        "<html><head><style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:16px 0;border-radius:12px}"
        "h3{margin:0 0 6px 0}"
        "</style></head><body>"
        "<h2>AI Employee Task Board</h2>"
        + cards +
        "<a href='/dashboard' style='color:#93c5fd'>← Back</a>"
        "</body></html>"
    )

    if not cards:
        cards = "<div class='card'>No actionable tasks</div>"

    return HTMLResponse(
        "<html><head><style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:16px 0;border-radius:12px}"
        "h3{margin:0 0 6px 0}"
        "</style></head><body>"
        "<h2>AI Employee Task Board</h2>"
        + cards +
        "<a href='/dashboard' style='color:#93c5fd'>← Back</a>"
        "</body></html>"
    )


    rows = ""
    for t in tasks:
        rows += (
            "<div class='card'>"
            f"<b>Task:</b> {t['task_type']}<br>"
            f"<b>Lead ID:</b> {t['lead_id'] or '-'}<br>"
            f"<b>Status:</b> {t['status']}<br>"
            f"<b>Run At:</b> {t['run_at'] or 'now'}<br>"
            f"<pre style='white-space:pre-wrap'>{t['payload'] or ''}</pre>"
            "</div>"
        )

    if not rows:
        rows = "<div class='card'>No ready tasks</div>"

    return HTMLResponse(
        "<html><head><style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:16px 0;border-radius:12px}"
        "</style></head><body>"
        "<h2>AI Task Queue</h2>"
        + rows +
        "<a href='/dashboard' style='color:#93c5fd'>← Back</a>"
        "</body></html>"
    )
AI_AUTOMATIONS_ENABLED = os.getenv("AI_AUTOMATIONS_ENABLED", "false").lower() == "true"
AI_DRY_RUN = os.getenv("AI_DRY_RUN", "true").lower() == "true"

@app.post("/tasks/execute")
def execute_tasks():
    if not AI_AUTOMATIONS_ENABLED:
        return {"ok": False, "reason": "AI automations disabled"}

    tasks = fetch_ready_tasks(limit=5)
    executed = []

    for task in tasks:
        task_id = task["id"]
        task_type = task["task_type"]

        if AI_DRY_RUN:
            mark_task_status(task_id, "SKIPPED")
            executed.append({
                "id": task_id,
                "task": task_type,
                "dry_run": True
            })
            continue

        # 🔥 REAL EXECUTION WILL GO HERE LATER
        # CALL / TEXT / FOLLOW_UP

        mark_task_status(task_id, "DONE")
        executed.append({
            "id": task_id,
            "task": task_type,
            "dry_run": False
        })

    return {"ok": True, "executed": executed}

# ==============================
# DASHBOARD
# ==============================
@app.get("/dashboard")
def dashboard():
    db = SessionLocal()

    leads = (
        db.query(Lead)
        .order_by(Lead.created_at.desc())
        .limit(50)
        .all()
    )

    db.close()

    rows = ""
    for l in leads:
        rows += (
            "<div class='card'>"
            f"<b>{l.full_name}</b><br>"
            f"{l.phone}<br>"
            f"{l.email or ''}<br>"
            f"<strong>AI State:</strong> {l.state}<br>"
            f"<strong>Next Action:</strong> {l.ai_next_action or ''}"
            "</div>"
        )

    if not rows:
        rows = "<div class='card'>No leads yet</div>"

    return HTMLResponse(
        "<html><head>"
        "<meta charset='utf-8'>"

        "<link rel='manifest' href='/manifest.json'>"
        "<meta name='theme-color' content='#0b0f17'>"
        "<link rel='apple-touch-icon' href='/static/icons/icon-192.png'>"
        "<meta name='apple-mobile-web-app-capable' content='yes'>"
        "<meta name='apple-mobile-web-app-status-bar-style' content='black-translucent'>"

        "<style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:16px 0;border-radius:12px}"
        "input,button{padding:10px;width:100%;margin:6px 0}"
        "button{background:#2563eb;color:white;border:none}"
        "</style>"
        "</head><body>"

        "<div class='card'>"
        "<h3>Add Lead</h3>"
        "<form method='post' action='/leads/create'>"
        "<input name='name' placeholder='Full Name' required>"
        "<input name='phone' placeholder='Phone' required>"
        "<input name='email' placeholder='Email'>"
        "<button>Add Lead</button>"
        "</form>"
        "</div>"

        "<div class='card'>"
        "<h3>Bulk Upload (CSV)</h3>"
        "<form method='post' action='/leads/upload' enctype='multipart/form-data'>"
        "<input type='file' name='file' accept='.csv' required>"
        "<button>Upload</button>"
        "</form>"
        "</div>"

        + rows +

        "<script>"
        "if ('serviceWorker' in navigator) {"
        "navigator.serviceWorker.register('/sw.js');"
        "}"
        "</script>"

        "</body></html>"
    )

# ==============================
# LEAD CREATION
# ==============================
@app.post("/leads/create")
def create_lead(
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form("")
):
    db = SessionLocal()
    lead = Lead(
        full_name=name,
        phone=normalize_phone(phone),
        email=email or None,
        state="NEW"
    )
    db.add(lead)
    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=302)

# ==============================
# BULK CSV UPLOAD
# ==============================
@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    rows = [r for r in csv.reader(raw) if any(c.strip() for c in r)]

    db = SessionLocal()
    imported = 0

    for r in rows:
        values = [c.strip() for c in r if c.strip()]

        name = next((v for v in values if looks_like_name(v)), "")
        phone = next((v for v in values if looks_like_phone(v)), "")
        email = next((v for v in values if looks_like_email(v)), "")

        if not name or not phone:
            continue

        lead = Lead(
            full_name=name,
            phone=normalize_phone(phone),
            email=email or None,
            state="NEW"
        )

        db.add(lead)
        imported += 1

    db.commit()
    db.close()

    return HTMLResponse(
        "<html><body>"
        f"<h3>Imported: {imported}</h3>"
        "<a href='/dashboard'>Back</a>"
        "</body></html>"
    )
