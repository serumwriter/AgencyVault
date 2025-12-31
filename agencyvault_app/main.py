from fastapi import FastAPI, UploadFile, File, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy import text
from datetime import datetime
import csv, os, re

from .database import SessionLocal, engine
from .models import Lead
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms
from ai_tasks import create_task

# ============================================================
# APP
# ============================================================
app = FastAPI(title="AgencyVault")

# ============================================================
# SCHEMA SAFETY (POSTGRES ONLY – NO MIGRATIONS)
# ============================================================
def ensure_schema():
    with engine.begin() as conn:
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

# ============================================================
# HELPERS
# ============================================================
def normalize_phone(s):
    if not s:
        return None
    d = re.sub(r"\D", "", s)
    return "+1" + d if len(d) == 10 else s

def looks_like_phone(s):
    return len(re.sub(r"\D", "", s or "")) == 10

def looks_like_name(s):
    parts = (s or "").split()
    return len(parts) >= 2 and all(p.isalpha() for p in parts)

def dedupe_exists(db, phone, email):
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

def clean_text(value: str | None):
    if not value:
        return None
    # Remove NULL bytes + non-printable control chars
    value = value.replace("\x00", "")
    value = re.sub(r"[\x00-\x1F\x7F]", "", value)
    return value.strip() or None
def base_styles():
    return """
    <style>
      body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}
      h1{font-size:32px;margin:0}
      .sub{opacity:.85;margin-bottom:20px}
      .row{display:flex;gap:12px;flex-wrap:wrap}
      .box{background:#020617;padding:18px;border-radius:12px;flex:1;min-width:280px}
      .card{background:#111827;padding:16px;margin:12px 0;border-radius:10px}
      input,textarea,button{padding:10px;margin:6px 0;width:100%;border-radius:8px;border:none}
      button{background:#2563eb;color:white;font-weight:700}
      .danger{background:#dc2626}
      .warn{background:#f59e0b;color:black}
      a{color:#60a5fa;text-decoration:none}
      .pill{padding:4px 10px;border-radius:999px;background:#0f172a;font-size:12px;margin-left:6px}
    </style>
    """

# ============================================================
# ROOT
# ============================================================
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

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
        pill = "<span class='pill'>HOT</span>" if l.needs_human else ""
        cards += f"""
        <div class="card">
          <b>{l.full_name or "Unnamed"} {pill}</b><br>
          📞 {l.phone}<br>
          ✉️ {l.email or "—"}<br>
          <a href="/leads/{l.id}">View Lead →</a>
        </div>
        """

    return HTMLResponse(f"""
    <html>
    <head>{base_styles()}</head>
    <body>
      <h1>AgencyVault</h1>
      <div class="sub">AI insurance employee — you only close.</div>

      <div class="row">
        <div class="box">
          <h3>Add Lead</h3>
          <form method="post" action="/leads/manual">
            <input name="full_name" placeholder="Name" />
            <input name="phone" placeholder="Phone" required />
            <input name="email" placeholder="Email" />
            <input name="source" placeholder="Source" />
            <button>Add</button>
          </form>
        </div>

        <div class="box">
          <h3>Upload CSV</h3>
          <form method="post" action="/leads/upload" enctype="multipart/form-data">
            <input type="file" name="file" required />
            <button>Upload</button>
          </form>
        </div>

        <div class="box">
          <h3>Live Ops</h3>
          <div>Total Leads: <b>{total}</b></div>
          <div>Needs You: <b>{hot}</b></div>
          <a href="/tasks">Open Tasks →</a><br>
          <a href="/ai/run">Run AI →</a>
        </div>
      </div>

      <h3>Recent Leads</h3>
      {cards}
    </body>
    </html>
    """)

# ============================================================
# LEAD CREATE / UPLOAD
# ============================================================
@app.post("/leads/manual")
def add_manual(full_name: str = Form(""), phone: str = Form(...), email: str = Form(""), source: str = Form("")):
    db = SessionLocal()
    p = normalize_phone(phone)
    e = email.strip() or None

    if not dedupe_exists(db, p, e):
        db.add(Lead(
            full_name=full_name or "Unknown",
            phone=p,
            email=e,
            source=source or None,
            status="New",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        ))
        db.commit()

    db.close()
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").splitlines()
    rows = csv.reader(raw)

    db = SessionLocal()
    for r in rows:
        vals = [c.strip() for c in r if c.strip()]
        phone = next((v for v in vals if looks_like_phone(v)), None)
        if not phone:
            continue

        p = normalize_phone(phone)
        if dedupe_exists(db, p, None):
            continue

        db.add(Lead(
            full_name=next((v for v in vals if looks_like_name(v)), "Unknown"),
            phone=p,
            status="New",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        ))

    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=303)

# ============================================================
# AI
# ============================================================
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead)
    base_url = os.getenv("BASE_URL", "").rstrip("/")

    for a in actions:
        create_task(a["type"], a["lead_id"])

        if a.get("needs_human"):
            lead = db.query(Lead).get(a["lead_id"])
            if lead:
                send_alert_sms(
                    f"🚨 AI NEEDS YOU\n"
                    f"{lead.full_name}\n"
                    f"{lead.phone}\n"
                    f"{base_url}/leads/{lead.id}"
                )

    db.close()
    return {"planned": len(actions)}

# ============================================================
# TASKS
# ============================================================
@app.get("/tasks", response_class=HTMLResponse)
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.id, t.task_type, t.lead_id, l.full_name, l.phone
        FROM ai_tasks t JOIN leads l ON l.id=t.lead_id
        WHERE t.status='NEW'
        ORDER BY t.created_at
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

    return HTMLResponse(f"<html><head>{base_styles()}</head><body><h2>Tasks</h2>{cards}</body></html>")


