print(">>> LOADED agencyvault_app/main.py <<<")

# -------------------------------------------------
# SAFE FLAGS
# -------------------------------------------------
TWILIO_ENABLED = False  # NEVER True during deploy

# -------------------------------------------------
# IMPORTS
# -------------------------------------------------
import os
import re
import csv
from datetime import datetime

from fastapi import FastAPI, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text
from sqlalchemy.orm import sessionmaker, declarative_base

# -------------------------------------------------
# CONFIG
# -------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

DATABASE_URL = DATABASE_URL.replace(
    "postgresql://", "postgresql+psycopg://"
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

# -------------------------------------------------
# MODELS (AI-FIRST)
# -------------------------------------------------
class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)

    full_name = Column(String(255))
    phone = Column(String(50), index=True)
    email = Column(String(255))

    # AI employee fields
    state = Column(String(30), default="NEW")
    ai_priority = Column(Integer, default=0)
    ai_next_action = Column(String(50))
    ai_reason = Column(Text)

    ai_last_action_at = Column(DateTime)
    ai_next_action_at = Column(DateTime)

    appointment_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# -------------------------------------------------
# APP
# -------------------------------------------------
app = FastAPI(title="AgencyVault")
app.add_middleware(SessionMiddleware, secret_key="CHANGE_ME")

# -------------------------------------------------
# HELPERS
# -------------------------------------------------
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

def page(title, body):
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
body {{ background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px }}
.card {{ background:#111827;padding:16px;margin:16px 0;border-radius:12px }}
input,button {{ padding:10px;width:100%;margin:6px 0 }}
button {{ background:#2563eb;color:white;border:none }}
</style>
</head>
<body>
{body}
</body>
</html>"""

# -------------------------------------------------
# AI EMPLOYEE
# -------------------------------------------------
from .ai_employee import run_ai_engine

# -------------------------------------------------
# ROUTES
# -------------------------------------------------
@app.get("/health")
def health():
    return {"ok": True}

@app.get("/ai/run")
def run_ai():
    db = SessionLocal()
    run_ai_engine(db, Lead)
    db.commit()
    db.close()
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/dashboard")
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).all()
    db.close()

    rows = ""
    for l in leads:
        rows += f"""
        <div class="card">
          <b>{l.full_name}</b><br>
          {l.phone}<br>
          {l.email or ""}
          <div style="margin-top:6px;">
            <strong>AI State:</strong> {l.state}<br>
            <strong>Next Action:</strong> {l.ai_next_action or ""}
          </div>
        </div>
        """

    if not rows:
        rows = "<div class='card'>No leads yet</div>"

    return HTMLResponse(f"""
          html = (
        "<html><body>"
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

    return HTMLResponse(html)




