from fastapi import FastAPI, UploadFile, File, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, text
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime
import csv
import os
import re

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
Base = declarative_base()

class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    full_name = Column(String)
    phone = Column(String)
    email = Column(String)

    # legacy + general
    source = Column(String, nullable=True)
    notes = Column(Text, nullable=True)

    # pipeline status (use this moving forward)
    status = Column(String, default="New")

    # AI guardrails
    product_interest = Column(String, default="UNKNOWN")  # LIFE/IUL/ANNUITY/UNKNOWN
    ai_confidence = Column(Integer, nullable=True)
    ai_evidence = Column(Text, nullable=True)
    ai_summary = Column(Text, nullable=True)
    needs_human = Column(Integer, default=0)

    # follow-up memory
    attempt_count = Column(Integer, default=0)
    last_contacted_at = Column(DateTime, nullable=True)
    next_followup_at = Column(DateTime, nullable=True)

    # Pre-qual (keep as strings for safety)
    state = Column(String, nullable=True)
    dob = Column(String, nullable=True)
    smoker = Column(String, nullable=True)
    height = Column(String, nullable=True)
    weight = Column(String, nullable=True)
    desired_coverage = Column(String, nullable=True)
    monthly_budget = Column(String, nullable=True)
    time_horizon = Column(String, nullable=True)
    health_notes = Column(Text, nullable=True)

    # compliance/safety
    do_not_contact = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def ensure_schema():
    """Adds missing columns safely in Postgres without migrations."""
    with engine.begin() as conn:
        # leads table columns (id/full_name/phone/email might already exist)
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS source TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS notes TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'New'"))

        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS product_interest TEXT DEFAULT 'UNKNOWN'"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS ai_confidence INTEGER"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS ai_evidence TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS ai_summary TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS needs_human INTEGER DEFAULT 0"))

        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS attempt_count INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_contacted_at TIMESTAMP"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS next_followup_at TIMESTAMP"))

        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS state TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS dob TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS smoker TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS height TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS weight TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS desired_coverage TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS monthly_budget TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS time_horizon TEXT"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS health_notes TEXT"))

        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS do_not_contact INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE leads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT NOW()"))

ensure_schema()

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
    parts = (s or "").strip().split()
    return len(parts) >= 2 and all(p.isalpha() for p in parts)

def dedupe_exists(db, phone, email):
    q = db.query(Lead)
    if phone:
        if q.filter(Lead.phone == phone).first():
            return True
    if email:
        if q.filter(Lead.email == email).first():
            return True
    return False

def base_styles():
    return """
    <style>
      body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}
      h1{font-size:32px;margin:0 0 6px 0}
      .sub{opacity:.85;margin-bottom:18px}
      .row{display:flex;gap:12px;flex-wrap:wrap}
      .box{background:#020617;padding:18px;border-radius:12px;margin:10px 0;flex:1;min-width:280px}
      .card{background:#111827;padding:16px;margin:12px 0;border-radius:10px}
      input,textarea,button,select{padding:10px;margin:6px 0;width:100%;border-radius:8px;border:none}
      button{background:#2563eb;color:white;font-weight:700;cursor:pointer}
      .danger{background:#dc2626}
      .warn{background:#f59e0b;color:black;font-weight:800}
      a{color:#60a5fa;text-decoration:none}
      .pill{display:inline-block;padding:4px 10px;border-radius:999px;background:#0f172a;margin-left:8px;font-size:12px;opacity:.95}
      .muted{opacity:.8}
    </style>
    """

# --------------------
# PWA BASICS (so /sw.js stops 404)
# --------------------
@app.get("/manifest.json")
def manifest():
    return JSONResponse({
        "name": "AgencyVault",
        "short_name": "AgencyVault",
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#0b0f17",
        "theme_color": "#0b0f17",
        "icons": []
    })

@app.get("/sw.js")
def sw():
    # Minimal service worker (caching later)
    js = "self.addEventListener('fetch', function(event) { });"
    return Response(content=js, media_type="application/javascript")

# --------------------
# ROUTES
# --------------------
@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/dashboard")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()

    total = db.execute(text("SELECT COUNT(*) FROM leads")).scalar() or 0
    hot = db.execute(text("SELECT COUNT(*) FROM leads WHERE needs_human=1 AND do_not_contact=0")).scalar() or 0
    dups = db.execute(text("SELECT COUNT(*) FROM leads WHERE status='DUPLICATE'")).scalar() or 0

    db.close()

    lead_cards = ""
    for l in leads:
        pill = ""
        if l.do_not_contact:
            pill = "<span class='pill'>DNC</span>"
        elif l.needs_human:
            pill = "<span class='pill' style='background:#7f1d1d'>HOT</span>"
        elif (l.product_interest or "").upper() in ["IUL", "ANNUITY"]:
            pill = f"<span class='pill'>{l.product_interest}</span>"

        lead_cards += f"""
        <div class="card">
          <b>{(l.full_name or "Unnamed Lead")}{pill}</b><br>
          📞 {l.phone}<br>
          ✉️ {l.email or "—"}<br>
          <span class="muted">Status: {l.status or "—"} | AI: {l.ai_confidence or "—"}/100</span><br>
          <a href="/leads/{l.id}">View Lead →</a>
        </div>
        """

    return HTMLResponse(f"""
    <html>
    <head>
      <title>AgencyVault</title>
      <link rel="manifest" href="/manifest.json">
      {base_styles()}
    </head>
    <body>
      <h1>AgencyVault</h1>
      <div class="sub">AI insurance employee — you only close.</div>

      <div class="row">
        <div class="box">
          <h3 style="margin:0 0 10px 0;">➕ Manual Add</h3>
          <form method="post" action="/leads/manual">
            <input name="full_name" placeholder="Full Name" />
            <input name="phone" placeholder="Phone (required)" required />
            <input name="email" placeholder="Email (optional)" />
            <input name="source" placeholder="Source (TikTok, FB, Referral…)" />
            <button>Add Lead</button>
          </form>
        </div>

        <div class="box">
          <h3 style="margin:0 0 10px 0;">📤 Upload CSV</h3>
          <form method="post" action="/leads/upload" enctype="multipart/form-data">
            <input type="file" name="file" required />
            <button>Upload CSV</button>
          </form>
          <div class="muted" style="margin-top:10px;">
            Auto-dedupe: phone/email duplicates are skipped.
          </div>
        </div>

        <div class="box">
          <h3 style="margin:0 0 10px 0;">📞 Live Ops</h3>
          <div>Total Leads: <b>{total}</b></div>
          <div>Needs You (HOT): <b>{hot}</b></div>
          <div>Duplicates: <b>{dups}</b></div>
          <div style="margin-top:10px;">
            <a href="/tasks">Open Tasks →</a><br>
            <a href="/ai/run">Run AI (Plan) →</a>
          </div>
        </div>
      </div>

      <h3>📋 Recent Leads</h3>
      {lead_cards}

      <p class="muted">Tip: Installable PWA is enabled (manifest + service worker). Full offline caching comes later.</p>
    </body>
    </html>
    """)

@app.post("/leads/manual")
def add_lead_manual(
    full_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form(""),
    source: str = Form("")
):
    db = SessionLocal()
    p = normalize_phone(phone.strip())
    e = email.strip() or None

    if dedupe_exists(db, p, e):
        db.close()
        return RedirectResponse("/dashboard", status_code=303)

    lead = Lead(
        full_name=(full_name.strip() or "Unknown"),
        phone=p,
        email=e,
        source=(source.strip() or None),
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
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    rows = csv.reader(raw)

    db = SessionLocal()
    count = 0
    skipped_dupes = 0

    for r in rows:
        vals = [c.strip() for c in r if c and c.strip()]
        name = next((v for v in vals if looks_like_name(v)), None)
        phone = next((v for v in vals if looks_like_phone(v)), None)
        email = next((v for v in vals if "@" in v), None)

        if not phone:
            continue

        p = normalize_phone(phone)
        e = (email or "").strip() or None

        if dedupe_exists(db, p, e):
            skipped_dupes += 1
            continue

        db.add(Lead(
            full_name=name or "Unknown",
            phone=p,
            email=e,
            status="New",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        count += 1

    db.commit()
    db.close()

    return HTMLResponse(
        f"<h3>Imported {count} leads</h3>"
        f"<p>Skipped duplicates: {skipped_dupes}</p>"
        "<a href='/dashboard'>Back</a>"
    )

@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int, request: Request):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    db.close()

    if not lead:
        return HTMLResponse("Lead not found", status_code=404)

    hot_banner = ""
    if lead.needs_human:
        hot_banner = "<div class='card' style='border-left:6px solid #dc2626'><b>🚨 HOT:</b> AI flagged this lead for you.</div>"

    return HTMLResponse(f"""
    <html>
    <head>
      <title>Lead</title>
      <link rel="manifest" href="/manifest.json">
      {base_styles()}
    </head>
    <body>
      <a href="/dashboard">← Dashboard</a> &nbsp; | &nbsp; <a href="/tasks">Tasks</a>

      {hot_banner}

      <div class="card">
        <h2 style="margin:0 0 6px 0;">{lead.full_name}</h2>
        <div class="muted">📞 {lead.phone} &nbsp; ✉️ {lead.email or "—"}</div>
        <div style="margin-top:10px;">
          <b>Product:</b> {lead.product_interest or "UNKNOWN"}<br>
          <b>AI Confidence:</b> {lead.ai_confidence or "—"}/100<br>
          <b>AI Evidence:</b> {lead.ai_evidence or "—"}<br>
          <b>AI Summary:</b> {lead.ai_summary or "—"}<br>
          <b>Status:</b> {lead.status or "—"}<br>
          <b>DNC:</b> {"YES" if lead.do_not_contact else "NO"}
        </div>
      </div>

      <div class="row">
        <div class="box">
          <h3 style="margin:0 0 10px 0;">🧠 Pre-Qual</h3>
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

        <div class="box">
          <h3 style="margin:0 0 10px 0;">🗂 Notes / Source</h3>
          <form method="post" action="/leads/{lead.id}/notes">
            <input name="source" placeholder="Source" value="{lead.source or ""}"/>
            <textarea name="notes" placeholder="Notes">{lead.notes or ""}</textarea>
            <button type="submit">Save Notes</button>
          </form>

          <h3 style="margin:16px 0 10px 0;">Actions</h3>
          <form method="post" action="/leads/{lead.id}/call"><button type="submit">📞 CALL (Dry Run)</button></form>
          <form method="post" action="/leads/{lead.id}/escalate/now"><button class="danger" type="submit">🔥 Escalate: Wants Coverage NOW</button></form>
          <form method="post" action="/leads/{lead.id}/escalate/problem"><button class="warn" type="submit">⚠️ Escalate: Confused/Complicated</button></form>
          <form method="post" action="/leads/{lead.id}/dnc"><button class="danger" type="submit">🚫 Toggle Do-Not-Contact</button></form>
        </div>
      </div>
    </body>
    </html>
    """)

@app.post("/leads/{lead_id}/notes")
def save_notes(
    lead_id: int,
    source: str = Form(""),
    notes: str = Form("")
):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if lead:
        lead.source = source.strip() or None
        lead.notes = notes.strip() or None
        lead.updated_at = datetime.utcnow()
        db.commit()
    db.close()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

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
        lead.smoker = (smoker.strip().upper() or None)
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

@app.post("/leads/{lead_id}/dnc")
def toggle_dnc(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if lead:
        lead.do_not_contact = 0 if lead.do_not_contact else 1
        lead.updated_at = datetime.utcnow()
        db.commit()
    db.close()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/call")
def call_lead_dry_run(lead_id: int):
    create_task("CALL", lead_id)
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
        lead.updated_at = datetime.utcnow()
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
        lead.updated_at = datetime.utcnow()
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
                msg = (
                    "🚨 AI NEEDS YOU\n"
                    f"Reason: {a['type']}\n"
                    f"Lead: {lead.full_name}\n"
                    f"Product: {lead.product_interest}\n"
                    f"Confidence: {lead.ai_confidence or a.get('confidence', '—')}/100\n"
                    f"Why: {lead.ai_evidence or a.get('evidence', '—')}\n"
                    f"📞 {lead.phone}\n"
                    f"👉 {base_url}/leads/{lead.id}"
                )
                send_alert_sms(msg)

    db.close()
    return {"planned": len(actions)}

@app.post("/tasks/{task_id}/done")
def task_done(task_id: int):
    with engine.begin() as conn:
        conn.execute(text("UPDATE ai_tasks SET status='DONE' WHERE id=:id"), {"id": task_id})
    return RedirectResponse("/tasks", status_code=303)

@app.get("/tasks", response_class=HTMLResponse)
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.id, t.task_type, t.lead_id, t.status, t.created_at, l.full_name, l.phone
        FROM ai_tasks t
        JOIN leads l ON l.id = t.lead_id
        WHERE t.status = 'NEW'
        ORDER BY
          CASE WHEN t.task_type LIKE 'ESCALATE%' THEN 0 ELSE 1 END,
          t.created_at ASC
        LIMIT 50
    """)).fetchall()
    db.close()

    cards = ""
    for r in rows:
        is_hot = (r.task_type or "").startswith("ESCALATE")
        border = "#dc2626" if is_hot else "#111827"

        cards += f"""
        <div class="card" style="border-left:6px solid {border}">
          <b>{r.task_type}</b><br>
          {r.full_name}<br>
          {r.phone}<br>
          <a href="/leads/{r.lead_id}">View Lead →</a><br><br>
          <form method="post" action="/tasks/{r.id}/done">
            <button type="submit">Mark Done</button>
          </form>
        </div>
        """

    return HTMLResponse(f"""
    <html>
    <head>
      <title>Tasks</title>
      {base_styles()}
    </head>
    <body>
      <h2>Tasks</h2>
      {cards if cards else "<div class='card'>No tasks right now.</div>"}
      <br><a href="/dashboard">← Back</a>
    </body>
    </html>
    """)

