from fastapi import FastAPI, UploadFile, File, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import csv
import os
import re

from .models import Base, Lead
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms
from ai_tasks import create_task

# --------------------
# DATABASE
# --------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

Base.metadata.create_all(bind=engine)

# --------------------
# APP
# --------------------
app = FastAPI(title="AgencyVault")

# --------------------
# HELPERS
# --------------------
def clean_text(val):
    if not val:
        return None
    return str(val).replace("\x00", "").strip()

def normalize_phone(s):
    s = clean_text(s) or ""
    d = re.sub(r"\D", "", s)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None

def looks_like_phone(s):
    return len(re.sub(r"\D", "", s or "")) in (10, 11)

def looks_like_name(s):
    parts = (s or "").split()
    return len(parts) >= 2

# --------------------
# ROUTES
# --------------------
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
    db.close()

    cards = ""
    for l in leads:
        cards += f"""
        <div style="background:#111827;padding:16px;margin:12px 0;border-radius:10px">
          <b>{l.full_name or "Unnamed Lead"}</b><br>
          📞 {l.phone or "—"}<br>
          ✉️ {l.email or "—"}<br>
          <a href="/leads/{l.id}">View Lead →</a>
        </div>
        """

    return HTMLResponse(f"""
    <html>
    <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
      <h1>AgencyVault</h1>
      <p>Your AI insurance employee</p>

      <h3>Add Lead</h3>
      <form method="post" action="/leads/manual">
        <input name="full_name" placeholder="Full Name"><br>
        <input name="phone" placeholder="Phone" required><br>
        <input name="email" placeholder="Email"><br>
        <button>Add Lead</button>
      </form>

      <h3>Upload CSV</h3>
      <form method="post" action="/leads/upload" enctype="multipart/form-data">
        <input type="file" name="file" required>
        <button>Upload</button>
      </form>

      <h3>Recent Leads</h3>
      {cards}

      <br><a href="/tasks">View Tasks</a>
    </body>
    </html>
    """)

@app.post("/leads/manual")
def add_lead_manual(
    full_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form("")
):
    db = SessionLocal()
    lead = Lead(
        full_name=clean_text(full_name),
        phone=normalize_phone(phone),
        email=clean_text(email),
        status="New",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(lead)
    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").replace("\x00", "")
    rows = csv.reader(raw.splitlines())

    db = SessionLocal()
    count = 0

    for r in rows:
        vals = [clean_text(c) for c in r if c]
        name = next((v for v in vals if looks_like_name(v)), None)
        phone = next((v for v in vals if looks_like_phone(v)), None)

        if not phone:
            continue

        db.add(Lead(
            full_name=name,
            phone=normalize_phone(phone),
            status="New",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        count += 1

    db.commit()
    db.close()

    return HTMLResponse(f"<h3>Imported {count}</h3><a href='/dashboard'>Back</a>")

@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    db.close()

    if not lead:
        return HTMLResponse("Lead not found", status_code=404)

    return HTMLResponse(f"""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
      <h2>{lead.full_name}</h2>
      <p>📞 {lead.phone}</p>
      <p>✉️ {lead.email}</p>

      <form method="post" action="/leads/{lead.id}/call">
        <button>📞 CALL (Dry Run)</button>
      </form>

      <br><a href="/dashboard">Back</a>
    </body></html>
    """)

@app.post("/leads/{lead_id}/call")
def call_lead(lead_id: int):
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead)

    for a in actions:
        create_task(a["type"], a["lead_id"])

        if a.get("needs_human"):
            lead = db.query(Lead).filter(Lead.id == a["lead_id"]).first()
            if lead:
                send_alert_sms(
                    f"🚨 AI NEEDS YOU\nLead: {lead.full_name}\n📞 {lead.phone}"
                )

    db.close()
    return {"planned": len(actions)}

@app.get("/tasks", response_class=HTMLResponse)
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.id, t.task_type, l.full_name, l.phone
        FROM ai_tasks t
        JOIN leads l ON l.id = t.lead_id
        WHERE t.status='NEW'
        ORDER BY t.created_at
        LIMIT 50
    """)).fetchall()
    db.close()

    cards = ""
    for r in rows:
        cards += f"""
        <div style="background:#111827;padding:16px;margin:12px 0;border-radius:10px">
          <b>{r.task_type}</b><br>
          {r.full_name}<br>
          {r.phone}
        </div>
        """

    return HTMLResponse(f"""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
      <h2>Tasks</h2>
      {cards or "No tasks"}
      <br><a href="/dashboard">Back</a>
    </body></html>
    """)



