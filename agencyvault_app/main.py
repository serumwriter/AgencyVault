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
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base

from ai_tasks import create_task
from .ai_employee import run_ai_engine

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

    ai_last_action_at = Column(DateTime)
    ai_next_action_at = Column(DateTime)
    appointment_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# ==============================
# APP
# ==============================
app = FastAPI(title="AgencyVault")
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
    parts = s.strip().split()
    if not (2 <= len(parts) <= 4):
        return False
    return all(p.isalpha() and p[0].isupper() for p in parts)

# ==============================
# ROUTES
# ==============================
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/ai/run")
def run_ai():
    """
    Called by the background worker.
    Plans AI actions only (no calls, no texts).
    """
    db = SessionLocal()

    actions = run_ai_engine(db, Lead, plan_only=True)

    created = 0
    for action in actions:
        create_task(
            task_type=action["type"],
            lead_id=action.get("lead_id"),
            payload=str(action),
        )
        created += 1

    db.close()
    return {"ok": True, "planned_tasks": created}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/dashboard")
def dashboard():
    db = SessionLocal()

    # 🔑 NEVER LOAD ALL LEADS
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
        "<html><head><style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:16px 0;border-radius:12px}"
        "input,button{padding:10px;width:100%;margin:6px 0}"
        "button{background:#2563eb;color:white;border:none}"
        "</style></head><body>"
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
        "</body></html>"
    )

@app.post("/leads/create")
def create_lead(name: str = Form(...), phone: str = Form(...), email: str = Form("")):
    db = SessionLocal()
    lead = Lead(
        full_name=name,
        phone=normalize_phone(phone),
        email=email or None,
        state="NEW",
        ai_reason=None,
    )
    db.add(lead)
    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=302)

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
            state="NEW",
            ai_reason=None,
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
