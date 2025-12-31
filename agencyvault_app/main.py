from fastapi import FastAPI, UploadFile, File, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, text
from sqlalchemy.orm import sessionmaker, declarative_base
from .twilio_client import send_alert_sms
from datetime import datetime
import csv
import os
import re

from .ai_employee import run_ai_engine
from ai_tasks import create_task

# --------------------
# DATABASE
# --------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://")

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    full_name = Column(String)
    phone = Column(String)
    email = Column(String)

    state = Column(String, default="NEW")
    ai_priority = Column(Integer)
    ai_next_action = Column(String)
    ai_reason = Column(Text)
    ai_last_action_at = Column(DateTime)

    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

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
# ROUTES
# --------------------
@app.post("/leads/{lead_id}/prequal")
def save_prequal(
    lead_id: int,
    state: str = Form(""),
    dob: str = Form(""),
    smoker: str = Form(""),
    height: str = Form(""),
    weight: str = Form(""),
    desired_coverage: str = Form(""),
    monthly_budget: str = Form(""),
    time_horizon: str = Form(""),
    health_notes: str = Form("")
):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if lead:
        lead.state = state.strip() or None
        lead.dob = dob.strip() or None
        lead.smoker = smoker.strip().upper() or None
        lead.height = height.strip() or None
        lead.weight = weight.strip() or None
        lead.desired_coverage = desired_coverage.strip() or None
        lead.monthly_budget = monthly_budget.strip() or None
        lead.time_horizon = time_horizon.strip() or None
        lead.health_notes = health_notes.strip() or None
        lead.updated_at = datetime.utcnow()
        db.commit()
    db.close()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/escalate/now")
def escalate_now(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    base_url = os.getenv("BASE_URL", "").rstrip("/")
    if lead:
        lead.needs_human = 1
        lead.ai_confidence = max(lead.ai_confidence or 0, 95)
        lead.ai_evidence = (lead.ai_evidence or "") + "; manual escalation: wants coverage now"
        lead.ai_summary = (lead.ai_summary or "") + " | MANUAL ESCALATE NOW"
        db.commit()

        create_task("ESCALATE_NOW", lead_id)
        send_alert_sms(
            "🔥 MANUAL ESCALATION\n"
            f"Lead: {lead.full_name}\n"
            f"📞 {lead.phone}\n"
            f"👉 {base_url}/leads/{lead.id}"
        )
    db.close()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/escalate/problem")
def escalate_problem(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    base_url = os.getenv("BASE_URL", "").rstrip("/")
    if lead:
        lead.needs_human = 1
        lead.ai_confidence = max(lead.ai_confidence or 0, 90)
        lead.ai_evidence = (lead.ai_evidence or "") + "; manual escalation: confused/upset/complicated"
        lead.ai_summary = (lead.ai_summary or "") + " | MANUAL ESCALATE PROBLEM"
        db.commit()

        create_task("ESCALATE_PROBLEM", lead_id)
        send_alert_sms(
            "⚠️ MANUAL ESCALATION\n"
            f"Lead: {lead.full_name}\n"
            f"📞 {lead.phone}\n"
            f"👉 {base_url}/leads/{lead.id}"
        )
    db.close()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)


@app.post("/leads/{lead_id}/call")
def call_lead_dry_run(lead_id: int):
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)
@app.post("/leads/manual")
def add_lead_manual(
    full_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form("")
):
    db = SessionLocal()

    lead = Lead(
        full_name=full_name.strip() or None,
        phone=phone.strip(),
        email=email.strip() or None,
        state="NEW"
    )

    db.add(lead)
    db.commit()
    db.close()

    return RedirectResponse("/dashboard", status_code=303)
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# ---------- DASHBOARD ----------
from fastapi import Form

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
    db.close()

    lead_cards = ""
    for l in leads:
        lead_cards += f"""
        <div class="card">
          <b>{l.full_name or "Unnamed Lead"}</b><br>
          📞 {l.phone}<br>
          ✉️ {l.email or "—"}<br>
          <a href="/leads/{l.id}" class="link">View Lead →</a>
        </div>
        """

    return HTMLResponse(f"""
    <html>
    <head>
      <title>AgencyVault Dashboard</title>
      <style>
        body {{
          background:#0b0f17;
          color:#e6edf3;
          font-family:system-ui;
          padding:20px;
        }}
        h1 {{
          font-size:32px;
          margin-bottom:6px;
        }}
        .sub {{
          opacity:0.8;
          margin-bottom:24px;
        }}
        .card {{
          background:#111827;
          padding:16px;
          margin:12px 0;
          border-radius:10px;
        }}
        .box {{
          background:#020617;
          padding:20px;
          border-radius:12px;
          margin-bottom:24px;
        }}
        input, button {{
          padding:10px;
          margin:6px 0;
          width:100%;
          border-radius:6px;
          border:none;
        }}
        button {{
          background:#2563eb;
          color:white;
          font-weight:600;
          cursor:pointer;
        }}
        .link {{
          display:inline-block;
          margin-top:8px;
          color:#60a5fa;
          text-decoration:none;
        }}
      </style>
    </head>

    <body>

      <h1>AgencyVault</h1>
      <div class="sub">Your AI insurance employee</div>

      <!-- MANUAL ADD LEAD -->
      <div class="box">
        <h3>➕ Manually Add Lead</h3>
        <form method="post" action="/leads/manual">
          <input name="full_name" placeholder="Full Name" />
          <input name="phone" placeholder="Phone Number" required />
          <input name="email" placeholder="Email (optional)" />
          <button>Add Lead</button>
        </form>
      </div>

      <!-- UPLOAD CSV -->
      <div class="box">
        <h3>📤 Upload Leads (CSV)</h3>
        <form method="post" action="/leads/upload" enctype="multipart/form-data">
          <input type="file" name="file" required />
          <button>Upload CSV</button>
        </form>
      </div>

      <!-- CALL NOW -->
      <div class="box">
        <h3>📞 Call Now</h3>
        <a class="link" href="/tasks">View Call Tasks →</a>
      </div>

      <h3>📋 Recent Leads</h3>
      {lead_cards}

    </body>
    </html>
    """)

# ---------- LEAD DETAIL ----------
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    db.close()

    if not lead:
        return HTMLResponse("Lead not found", status_code=404)

    return HTMLResponse(f"""
    <html>
    <head>
      <title>Lead</title>
      <style>
        body{{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}}
        .card{{background:#111827;padding:16px;margin:12px 0;border-radius:10px}}
        input,textarea,button{{width:100%;padding:10px;margin:6px 0;border-radius:8px;border:none}}
        button{{background:#2563eb;color:white;font-weight:700;cursor:pointer}}
        .danger{{background:#dc2626}}
        .warn{{background:#f59e0b;color:black}}
        .muted{{opacity:.8}}
        a{{color:#60a5fa;text-decoration:none}}
      </style>
    </head>
    <body>

      <div class="card">
        <h2 style="margin:0 0 6px 0;">{lead.full_name}</h2>
        <div class="muted">📞 {lead.phone} &nbsp; ✉️ {lead.email or "—"}</div>
        <div style="margin-top:10px;">
          <b>Product:</b> {lead.product_interest or "UNKNOWN"}<br>
          <b>AI Confidence:</b> {lead.ai_confidence or "—"}/100<br>
          <b>AI Evidence:</b> {lead.ai_evidence or "—"}<br>
          <b>AI Summary:</b> {lead.ai_summary or "—"}<br>
          <b>Status:</b> {lead.status}
        </div>
      </div>

      <div class="card">
        <h3 style="margin-top:0;">📞 CALL (Dry Run)</h3>
        <form method="post" action="/leads/{lead.id}/call">
          <button type="submit">Call Now (Dry Run)</button>
        </form>
      </div>

      <div class="card">
        <h3 style="margin-top:0;">🧠 Pre-Qual (AI collects this before you close)</h3>
        <form method="post" action="/leads/{lead.id}/prequal">
          <input name="state" placeholder="State" value="{lead.state or ""}"/>
          <input name="dob" placeholder="DOB (MM/DD/YYYY)" value="{lead.dob or ""}"/>
          <input name="smoker" placeholder="Smoker? YES / NO / UNKNOWN" value="{lead.smoker or ""}"/>
          <input name="height" placeholder="Height (e.g. 5'10)" value="{lead.height or ""}"/>
          <input name="weight" placeholder="Weight (e.g. 185)" value="{lead.weight or ""}"/>
          <input name="desired_coverage" placeholder="Desired Coverage (e.g. 500k)" value="{lead.desired_coverage or ""}"/>
          <input name="monthly_budget" placeholder="Monthly Budget (e.g. 80)" value="{lead.monthly_budget or ""}"/>
          <input name="time_horizon" placeholder="Time Horizon (ASAP / 30 days / shopping)" value="{lead.time_horizon or ""}"/>
          <textarea name="health_notes" placeholder="Health notes (conditions, meds, surgeries)">{lead.health_notes or ""}</textarea>
          <button type="submit">Save Pre-Qual</button>
        </form>
      </div>

      <div class="card">
        <h3 style="margin-top:0;">🚨 Escalate to Human (forces notification)</h3>
        <form method="post" action="/leads/{lead.id}/escalate/now">
          <button class="danger" type="submit">🔥 Wants Coverage NOW</button>
        </form>
        <form method="post" action="/leads/{lead.id}/escalate/problem">
          <button class="warn" type="submit">⚠️ Confused / Upset / Complicated</button>
        </form>
      </div>

      <p><a href="/dashboard">← Back to Dashboard</a> &nbsp; | &nbsp; <a href="/tasks">Tasks</a></p>

    </body>
    </html>
    """)


# ---------- CALL (DRY RUN) ----------
@app.post("/leads/{lead_id}/call")
def call_lead_dry_run(lead_id: int):
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

# ---------- ESCALATION ----------
@app.post("/leads/{lead_id}/escalate/now")
def escalate_now(lead_id: int):
    create_task("ESCALATE_NOW", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/escalate/problem")
def escalate_problem(lead_id: int):
    create_task("ESCALATE_PROBLEM", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

# ---------- CSV UPLOAD ----------
@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    rows = csv.reader(raw)

    db = SessionLocal()
    count = 0

    for r in rows:
        vals = [c.strip() for c in r if c.strip()]
        name = next((v for v in vals if looks_like_name(v)), None)
        phone = next((v for v in vals if looks_like_phone(v)), None)

        if not name or not phone:
            continue

        db.add(Lead(
            full_name=name,
            phone=normalize_phone(phone),
            state="NEW"
        ))
        count += 1

    db.commit()
    db.close()

    return HTMLResponse(f"<h3>Imported {count}</h3><a href='/dashboard'>Back</a>")

# ---------- AI PLAN ----------
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead)
    base_url = os.getenv("BASE_URL", "").rstrip("/")

    for a in actions:
        create_task(a["type"], a["lead_id"])

        # notify you for ANY AI reason that requires you
        if a.get("needs_human"):
            lead = db.query(Lead).filter(Lead.id == a["lead_id"]).first()
            if lead:
                msg = (
                    "🚨 AI NEEDS YOU\n"
                    f"Reason: {a['type']}\n"
                    f"Lead: {lead.full_name}\n"
                    f"Product: {lead.product_interest}\n"
                    f"Confidence: {lead.ai_confidence}/100\n"
                    f"Why: {lead.ai_evidence}\n"
                    f"📞 {lead.phone}\n"
                    f"👉 {base_url}/leads/{lead.id}"
                )
                send_alert_sms(msg)

    db.close()
    return {"planned": len(actions)}



# ---------- TASKS ----------
@app.get("/tasks")
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.task_type, t.lead_id, l.full_name, l.phone
        FROM ai_tasks t
        JOIN leads l ON l.id = t.lead_id
        WHERE t.status = 'NEW'
        ORDER BY
          CASE
            WHEN t.task_type LIKE 'ESCALATE%' THEN 0
            ELSE 1
          END,
          t.created_at
        LIMIT 50
    """)).fetchall()
    db.close()

    cards = ""
    for r in rows:
        color = "#dc2626" if r.task_type.startswith("ESCALATE") else "#111827"
        cards += (
            f"<div class='card' style='border-left:6px solid {color}'>"
            f"<b>{r.task_type}</b><br>"
            f"{r.full_name}<br>"
            f"{r.phone}<br>"
            f"<a href='/leads/{r.lead_id}'>View Lead →</a>"
            "</div>"
        )

    return HTMLResponse(
        "<html><head><style>"
        "body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}"
        ".card{background:#111827;padding:16px;margin:12px 0;border-radius:10px}"
        "</style></head><body>"
        "<h2>Tasks</h2>"
        + cards +
        "<br><a href='/dashboard'>Back</a>"
        "</body></html>"
    )
