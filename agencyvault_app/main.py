import os
import csv
import re
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text

from .database import SessionLocal
from .models import Lead
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms
from ai_tasks import create_task

# --------------------
# APP
# --------------------
app = FastAPI()

# --------------------
# HELPERS
# --------------------
def normalize_phone(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 10:
        return "+1" + d
    return d

def looks_like_phone(s):
    return len(re.sub(r"\D", "", s or "")) == 10

def looks_like_name(s):
    parts = s.split()
    return len(parts) >= 2 and all(p.isalpha() for p in parts)

# --------------------
# ROOT
# --------------------
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# --------------------
# DASHBOARD
# --------------------
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
    db.close()

    cards = ""
    for l in leads:
        cards += f"""
        <div class="card">
          <b>{l.full_name or "Unnamed Lead"}</b><br>
          📞 {l.phone}<br>
          ✉️ {l.email or "—"}<br>
          <a href="/leads/{l.id}">View Lead →</a>
        </div>
        """

    return HTMLResponse(f"""
    <html>
    <head>
      <title>AgencyVault</title>
      <style>
        body{{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}}
        .card{{background:#111827;padding:16px;margin:12px 0;border-radius:10px}}
        .box{{background:#020617;padding:20px;border-radius:12px;margin-bottom:20px}}
        input,button{{width:100%;padding:10px;margin:6px 0;border-radius:6px;border:none}}
        button{{background:#2563eb;color:white;font-weight:600}}
        a{{color:#60a5fa;text-decoration:none}}
      </style>
    </head>
    <body>

      <h1>AgencyVault</h1>
      <p>Your AI insurance employee</p>

      <div class="box">
        <h3>➕ Add Lead</h3>
        <form method="post" action="/leads/manual">
          <input name="full_name" placeholder="Full Name" />
          <input name="phone" placeholder="Phone" required />
          <input name="email" placeholder="Email" />
          <button>Add Lead</button>
        </form>
      </div>

      <div class="box">
        <h3>📤 Upload CSV</h3>
        <form method="post" action="/leads/upload" enctype="multipart/form-data">
          <input type="file" name="file" required />
          <button>Upload</button>
        </form>
      </div>

      <div class="box">
        <h3>📞 Call Queue</h3>
        <a href="/tasks">View Tasks →</a>
      </div>

      <h3>Recent Leads</h3>
      {cards}

    </body>
    </html>
    """)

# --------------------
# MANUAL LEAD
# --------------------
@app.post("/leads/manual")
def add_lead_manual(
    full_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form("")
):
    db = SessionLocal()
    db.add(Lead(
        full_name=full_name.strip() or None,
        phone=normalize_phone(phone),
        email=email.strip() or None,
        status="New"
    ))
    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=303)

# --------------------
# CSV UPLOAD
# --------------------
@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    reader = csv.reader(raw)

    db = SessionLocal()
    count = 0

    for r in reader:
        vals = [c.strip() for c in r if c.strip()]
        name = next((v for v in vals if looks_like_name(v)), None)
        phone = next((v for v in vals if looks_like_phone(v)), None)
        if not phone:
            continue

        db.add(Lead(
            full_name=name,
            phone=normalize_phone(phone),
            status="New"
        ))
        count += 1

    db.commit()
    db.close()
    return HTMLResponse(f"<h3>Imported {count}</h3><a href='/dashboard'>Back</a>")

# --------------------
# LEAD DETAIL
# --------------------
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    db.close()

    if not lead:
        return HTMLResponse("Not found", status_code=404)

    return HTMLResponse(f"""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
      <h2>{lead.full_name}</h2>
      <p>📞 {lead.phone}</p>
      <p>✉️ {lead.email or "—"}</p>

      <p><b>Product:</b> {lead.product_interest}</p>
      <p><b>Confidence:</b> {lead.ai_confidence}</p>
      <p><b>Evidence:</b> {lead.ai_evidence}</p>

      <form method="post" action="/leads/{lead.id}/call">
        <button>📞 Call Now (Dry Run)</button>
      </form>

      <form method="post" action="/leads/{lead.id}/escalate/now">
        <button style="background:#dc2626">🔥 Escalate Now</button>
      </form>

      <p><a href="/dashboard">← Back</a></p>
    </body></html>
    """)

# --------------------
# ACTIONS
# --------------------
@app.post("/leads/{lead_id}/call")
def call_lead(lead_id: int):
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/escalate/now")
def escalate_now(lead_id: int):
    create_task("ESCALATE_NOW", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

# --------------------
# AI RUN
# --------------------
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead)
    base_url = os.getenv("BASE_URL", "").rstrip("/")

    for a in actions:
        create_task(a["type"], a["lead_id"])
        if a.get("needs_human"):
            lead = db.query(Lead).filter(Lead.id == a["lead_id"]).first()
            if lead:
                send_alert_sms(
                    "🚨 AI NEEDS YOU\n"
                    f"Lead: {lead.full_name}\n"
                    f"Product: {lead.product_interest}\n"
                    f"Confidence: {lead.ai_confidence}\n"
                    f"Why: {lead.ai_evidence}\n"
                    f"📞 {lead.phone}\n"
                    f"👉 {base_url}/leads/{lead.id}"
                )
    db.close()
    return {"planned": len(actions)}

# --------------------
# TASKS
# --------------------
@app.get("/tasks", response_class=HTMLResponse)
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.task_type, t.lead_id, l.full_name, l.phone
        FROM ai_tasks t
        JOIN leads l ON l.id = t.lead_id
        WHERE t.status = 'NEW'
        ORDER BY
          CASE WHEN t.task_type LIKE 'ESCALATE%' THEN 0 ELSE 1 END,
          t.created_at
        LIMIT 50
    """)).fetchall()
    db.close()

    cards = ""
    for r in rows:
        cards += f"""
        <div class="card">
          <b>{r.task_type}</b><br>
          {r.full_name}<br>
          {r.phone}<br>
          <a href="/leads/{r.lead_id}">View Lead →</a>
        </div>
        """

    return HTMLResponse(
        "<html><body style='background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px'>"
        "<h2>Tasks</h2>"
        + cards +
        "<br><a href='/dashboard'>Back</a>"
        "</body></html>"
    )
