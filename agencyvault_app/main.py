from fastapi import FastAPI, UploadFile, File, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy import text
from datetime import datetime
import csv
import os
import re

from .database import SessionLocal, engine
from .models import Lead
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms
from ai_tasks import create_task

app = FastAPI(title="AgencyVault")

# ============================================================
# HARD SANITIZATION (fixes Postgres NUL-byte crashes permanently)
# ============================================================
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def clean_text(val):
    if not val:
        return None
    if not isinstance(val, str):
        val = str(val)
    return _CONTROL_RE.sub("", val).replace("\x00", "").strip() or None

def normalize_phone(val):
    val = clean_text(val) or ""
    digits = re.sub(r"\D", "", val)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None

def looks_like_phone(val):
    val = clean_text(val) or ""
    d = re.sub(r"\D", "", val)
    return len(d) in (10, 11)

def looks_like_name(val):
    val = clean_text(val) or ""
    parts = val.split()
    return len(parts) >= 2 and all(p.replace("-", "").isalpha() for p in parts)

def dedupe_exists(db, phone, email):
    phone = clean_text(phone)
    email = clean_text(email)
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

# ============================================================
# PWA (kills /sw.js errors)
# ============================================================
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
    return Response(
        content="self.addEventListener('fetch',()=>{});",
        media_type="application/javascript"
    )

# ============================================================
# BASIC ROUTES
# ============================================================
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/health")
def health():
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}

# ============================================================
# DASHBOARD
# ============================================================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()

    leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
    total = db.execute(text("SELECT COUNT(*) FROM leads")).scalar() or 0
    hot = db.execute(text("SELECT COUNT(*) FROM leads WHERE needs_human=1")).scalar() or 0

    db.close()

    cards = ""
    for l in leads:
        badge = ""
        if getattr(l, "needs_human", 0):
            badge = "<span style='color:#f87171'> HOT</span>"
        cards += f"""
        <div style="background:#111827;padding:14px;margin:10px 0;border-radius:8px">
          <b>{l.full_name or "Unnamed"}{badge}</b><br>
          📞 {l.phone or "—"}<br>
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
        input,button{{padding:10px;width:100%;margin:6px 0}}
        button{{background:#2563eb;color:white;font-weight:700}}
        a{{color:#60a5fa;text-decoration:none}}
      </style>
    </head>
    <body>
      <h1>AgencyVault</h1>
      <p>AI insurance employee — you only close.</p>

      <h3>Add Lead</h3>
      <form method="post" action="/leads/manual">
        <input name="full_name" placeholder="Full Name">
        <input name="phone" placeholder="Phone" required>
        <input name="email" placeholder="Email">
        <button>Add Lead</button>
      </form>

      <h3>Upload CSV</h3>
      <form method="post" action="/leads/upload" enctype="multipart/form-data">
        <input type="file" name="file" required>
        <button>Upload</button>
      </form>

      <p>Total Leads: <b>{total}</b> | Needs You: <b>{hot}</b></p>
      <a href="/tasks">View Tasks →</a>

      <h3>Recent Leads</h3>
      {cards}
    </body>
    </html>
    """)

# ============================================================
# LEADS
# ============================================================
@app.post("/leads/manual")
def add_lead_manual(
    full_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form("")
):
    db = SessionLocal()
    p = normalize_phone(phone)
    e = clean_text(email)

    if not p or dedupe_exists(db, p, e):
        db.close()
        return RedirectResponse("/dashboard", status_code=303)

    db.add(Lead(
        full_name=clean_text(full_name) or "Unknown",
        phone=p,
        email=e,
        status="New",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    ))
    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").replace("\x00", "")
    rows = csv.reader(raw.splitlines())

    db = SessionLocal()
    added = 0

    for r in rows:
        vals = [clean_text(c) for c in r if clean_text(c)]
        phone = next((v for v in vals if looks_like_phone(v)), None)
        name = next((v for v in vals if looks_like_name(v)), None)
        email = next((v for v in vals if "@" in v), None)

        p = normalize_phone(phone)
        e = clean_text(email)

        if not p or dedupe_exists(db, p, e):
            continue

        db.add(Lead(
            full_name=name or "Unknown",
            phone=p,
            email=e,
            status="New",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        ))
        added += 1

    db.commit()
    db.close()
    return HTMLResponse(f"<h3>Imported {added}</h3><a href='/dashboard'>Back</a>")

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
      📞 {lead.phone}<br>
      ✉️ {lead.email or "—"}<br>
      Status: {lead.status}<br><br>

      <form method="post" action="/leads/{lead.id}/call">
        <button>📞 CALL (Dry Run)</button>
      </form>

      <form method="post" action="/leads/{lead.id}/escalate">
        <button style="background:#dc2626">🚨 Escalate</button>
      </form>

      <br><a href="/dashboard">← Back</a>
    </body></html>
    """)

@app.post("/leads/{lead_id}/call")
def call_lead(lead_id: int):
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/escalate")
def escalate(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    base_url = os.getenv("BASE_URL", "")

    if lead:
        lead.needs_human = 1
        lead.updated_at = datetime.utcnow()
        db.commit()

        create_task("ESCALATE", lead_id)
        send_alert_sms(
            f"🚨 AI NEEDS YOU\n{lead.full_name}\n{lead.phone}\n{base_url}/leads/{lead.id}"
        )

    db.close()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

# ============================================================
# AI + TASKS
# ============================================================
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead)
    for a in actions:
        create_task(a["type"], a["lead_id"])
    db.close()
    return {"planned": len(actions)}

@app.get("/tasks", response_class=HTMLResponse)
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.id, t.task_type, l.full_name, l.phone
        FROM ai_tasks t
        JOIN leads l ON l.id=t.lead_id
        WHERE t.status='NEW'
        ORDER BY t.created_at
    """)).fetchall()
    db.close()

    cards = ""
    for r in rows:
        cards += f"""
        <div style="background:#111827;padding:14px;margin:10px 0;border-radius:8px">
          <b>{r.task_type}</b><br>
          {r.full_name}<br>
          {r.phone}<br>
          <a href="/leads/{r.id}">View Lead →</a>
        </div>
        """

    return HTMLResponse(f"""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
      <h2>Tasks</h2>
      {cards or "No tasks"}
      <br><a href="/dashboard">Back</a>
    </body></html>
    """)



