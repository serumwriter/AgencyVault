# agencyvault_app/main.py

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy import text
from datetime import datetime
import csv
import os
import re
import json

# ✅ PACKAGE-LOCAL IMPORTS (LOCKED — DO NOT CHANGE)
from .database import SessionLocal, engine
from .models import Lead, LeadMemory

# Existing app modules (keep as-is)
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms
from ai_tasks import create_task


app = FastAPI(title="AgencyVault")

# ============================================================
# HARD SANITIZATION (prevents Postgres NUL/control-byte issues)
# ============================================================
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def clean_text(val):
    if val is None:
        return None
    if not isinstance(val, str):
        val = str(val)
    val = _CONTROL_RE.sub("", val).replace("\x00", "").strip()
    return val or None

def normalize_phone(s: str | None):
    s = clean_text(s) or ""
    d = re.sub(r"\D", "", s)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    # return None to avoid inserting garbage
    return None

def looks_like_phone(s: str | None):
    d = re.sub(r"\D", "", clean_text(s) or "")
    return len(d) in (10, 11)

def looks_like_name(s: str | None):
    s = clean_text(s) or ""
    parts = s.split()
    if len(parts) < 2:
        return False
    # allow hyphens
    return all(p.replace("-", "").isalpha() for p in parts)

def dedupe_exists(db, phone: str | None, email: str | None):
    phone = clean_text(phone)
    email = clean_text(email)
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

# ============================================================
# LeadMemory helpers (keeps "needs_human" feature WITHOUT schema break)
# ============================================================
def mem_get(db, lead_id: int, key: str) -> str | None:
    row = (
        db.query(LeadMemory)
        .filter(LeadMemory.lead_id == lead_id, LeadMemory.key == key)
        .first()
    )
    return row.value if row else None

def mem_set(db, lead_id: int, key: str, value: str):
    row = (
        db.query(LeadMemory)
        .filter(LeadMemory.lead_id == lead_id, LeadMemory.key == key)
        .first()
    )
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(LeadMemory(lead_id=lead_id, key=key, value=value, updated_at=datetime.utcnow()))

def needs_human(db, lead_id: int) -> bool:
    return (mem_get(db, lead_id, "needs_human") or "0") == "1"

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
    # DB ping
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True, "service": "AgencyVault", "time": datetime.utcnow().isoformat()}

# ============================================================
# DASHBOARD
# ============================================================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
        total = db.execute(text("SELECT COUNT(*) FROM leads")).scalar() or 0

        # Count "needs human" via LeadMemory
        hot = db.execute(text("""
            SELECT COUNT(*)
            FROM lead_memory
            WHERE key='needs_human' AND value='1'
        """)).scalar() or 0

        cards = ""
        for l in leads:
            badge = "<span style='color:#f87171'> HOT</span>" if needs_human(db, l.id) else ""
            cards += f"""
            <div style="background:#111827;padding:16px;margin:12px 0;border-radius:10px">
              <b>{(l.full_name or "Unnamed Lead")}{badge}</b><br>
              📞 {l.phone or "—"}<br>
              ✉️ {l.email or "—"}<br>
              <div style="margin-top:8px;">
                <a href="/leads/{l.id}">View Lead →</a>
              </div>
            </div>
            """

        return HTMLResponse(f"""
        <html>
          <head>
            <title>AgencyVault</title>
            <style>
              body{{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}}
              input,button{{padding:10px;width:100%;margin:6px 0}}
              button{{background:#2563eb;color:white;font-weight:700;border:none;border-radius:10px}}
              a{{color:#60a5fa;text-decoration:none}}
              .row{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}
              .card{{background:#0f172a;padding:14px;border-radius:12px}}
              @media(max-width:900px){{.row{{grid-template-columns:1fr}}}}
            </style>
          </head>
          <body>
            <h1>AgencyVault</h1>
            <p>AI insurance employee — you only close.</p>

            <div class="row">
              <div class="card">
                <h3>Add Lead</h3>
                <form method="post" action="/leads/manual">
                  <input name="full_name" placeholder="Full Name">
                  <input name="phone" placeholder="Phone" required>
                  <input name="email" placeholder="Email">
                  <button>Add Lead</button>
                </form>
              </div>

              <div class="card">
                <h3>Upload CSV (bulk)</h3>
                <form method="post" action="/upload" enctype="multipart/form-data">
                  <input type="file" name="file" required>
                  <button>Upload</button>
                </form>
              </div>
            </div>

            <div style="margin-top:16px" class="card">
              <p>Total Leads: <b>{total}</b> | Needs You: <b>{hot}</b></p>
              <div style="display:flex;gap:12px;flex-wrap:wrap">
                <a href="/tasks">View Tasks →</a>
                <a href="/ai/run">Run AI (Planning) →</a>
              </div>
            </div>

            <h3 style="margin-top:18px">Recent Leads</h3>
            {cards if cards else "<p>No leads yet.</p>"}
          </body>
        </html>
        """)
    finally:
        db.close()

# ============================================================
# LEADS
# ============================================================
mem = {
    m.key: m.value
    for m in db.query(LeadMemory)
        .filter(LeadMemory.lead_id == lead.id)
        .all()
}
<h3> AI Lead Profile</h3>
<ul>
  <li>State: {{ mem.get("state","—") }}</li>
  <li>Smoker: {{ mem.get("smoker","—") }}</li>
  <li>Medical: {{ mem.get("medical","—") }}</li>
  <li>Income: {{ mem.get("income","—") }}</li>
  <li>Product Interest: {{ lead.product_interest or "—" }}</li>
</ul>
@app.post("/leads/manual")
def add_lead_manual(
    full_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form("")
):
    db = SessionLocal()
    try:
        p = normalize_phone(phone)
        e = clean_text(email)
        n = clean_text(full_name) or "Unknown"

        if not p:
            return RedirectResponse("/dashboard", status_code=303)

        if dedupe_exists(db, p, e):
            return RedirectResponse("/dashboard", status_code=303)

        lead = Lead(
            full_name=n,
            phone=p,
            email=e,
            state="NEW",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(lead)
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.post("/upload", response_class=HTMLResponse)
async def upload(file: UploadFile = File(...)):
    raw_bytes = await file.read()
    raw = raw_bytes.decode("utf-8", errors="ignore")
    rows = csv.reader(raw.splitlines())

    db = SessionLocal()
    try:
        added = 0
        count = 0

        for r in rows:
            vals = [clean_text(c) for c in r if clean_text(c)]
            phone_raw = next((v for v in vals if looks_like_phone(v)), None)
            name = next((v for v in vals if looks_like_name(v)), None)
            email = next((v for v in vals if v and "@" in v), None)

            p = normalize_phone(phone_raw)
            e = clean_text(email)
            n = clean_text(name) or "Unknown"

            if not p:
                continue

            if dedupe_exists(db, p, e):
                continue

            db.add(Lead(
                full_name=n,
                phone=p,
                email=e,
                state="NEW",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))
            added += 1
            count += 1

        db.commit()
        return HTMLResponse(f"<h3>Imported {added} leads</h3><a href='/dashboard'>Back</a>")
    finally:
        db.close()

@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            return HTMLResponse("Lead not found", status_code=404)

        hot = needs_human(db, lead.id)

        return HTMLResponse(f"""
        <html>
          <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
            <h2>{lead.full_name or "Unnamed Lead"}</h2>
            <p>📞 {lead.phone or "—"}</p>
            <p>✉️ {lead.email or "—"}</p>
            <p>Status: {lead.state}</p>
            <p>Needs you: {"YES" if hot else "no"}</p>

            <form method="post" action="/leads/{lead.id}/call">
              <button>📞 CALL (Dry Run)</button>
            </form>

            <form method="post" action="/leads/{lead.id}/escalate">
              <button style="background:#dc2626">🚨 Escalate</button>
            </form>

            <br><a href="/dashboard">← Back</a>
          </body>
        </html>
        """)
    finally:
        db.close()

@app.post("/leads/{lead_id}/call")
def call_lead(lead_id: int):
    # Your existing task system
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/escalate")
def escalate(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter(Lead.id == lead_id).first()
        base_url = os.getenv("BASE_URL", "")

        if lead:
            # Store "needs human" safely in LeadMemory
            mem_set(db, lead.id, "needs_human", "1")
            lead.updated_at = datetime.utcnow()
            db.commit()

            create_task("ESCALATE", lead.id)

            # Twilio alert
            send_alert_sms(
                f"🚨 AI NEEDS YOU\n{lead.full_name}\n{lead.phone}\n{base_url}/leads/{lead.id}"
            )

        return RedirectResponse(f"/leads/{lead_id}", status_code=303)
    finally:
        db.close()

# ============================================================
# AI + TASKS
# ============================================================
from datetime import datetime, timedelta

BUSINESS_START = 9
BUSINESS_END = 18
MIN_GAP_MINUTES = 30


def next_business_time(now: datetime) -> datetime:
    t = now + timedelta(minutes=MIN_GAP_MINUTES)

    if t.hour < BUSINESS_START:
        t = t.replace(hour=BUSINESS_START, minute=0)
    elif t.hour >= BUSINESS_END:
        t = (t + timedelta(days=1)).replace(hour=BUSINESS_START, minute=0)

    return t


@app.get("/ai/run")
def ai_run():
    """
    Planning-mode AI:
    - Generates tasks with schedule
    - Avoids re-planning same lead
    - Escalates only when necessary
    """
    db = SessionLocal()
    try:
        actions = run_ai_engine(db, Lead) or []

        planned = 0
        escalations = 0
        now = datetime.utcnow()

        for a in actions:
            t = a.get("type")
            lead_id = a.get("lead_id")

            if not t or not lead_id:
                continue

            # 🔒 Do not re-plan same action too often
            last_plan = mem_get(db, lead_id, f"last_plan_{t}")
            if last_plan:
                try:
                    last_plan_dt = datetime.fromisoformat(last_plan)
                    if last_plan_dt + timedelta(hours=12) > now:
                        continue
                except Exception:
                    pass

            due_at = a.get("due_at") or next_business_time(now)

            # ✅ Create scheduled task
            create_task(
                task_type=t,
                lead_id=lead_id,
                due_at=due_at
            )
            planned += 1

            # 🧠 Remember planning
            mem_set(db, lead_id, f"last_plan_{t}", now.isoformat())

            # 🚨 Escalation logic
            if a.get("needs_human"):
                lead = db.query(Lead).filter(Lead.id == lead_id).first()
                if lead:
                    mem_set(db, lead.id, "needs_human", "1")
                    lead.updated_at = now
                    escalations += 1

                    send_alert_sms(
                        f"🚨 AI NEEDS YOU\n"
                        f"Lead: {lead.full_name}\n"
                        f"📞 {lead.phone}"
                    )

        db.commit()
        return {
            "planned": planned,
            "escalations": escalations,
            "timestamp": now.isoformat()
        }

    finally:
        db.close()

@app.get("/tasks", response_class=HTMLResponse)
def tasks():
    """
    Keeps your existing ai_tasks table behavior.
    If your schema differs, we’ll adjust the query after we confirm it.
    """
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT t.id, t.task_type, l.full_name, l.phone, t.lead_id
            FROM ai_tasks t
            JOIN leads l ON l.id = t.lead_id
            WHERE t.status='NEW'
            ORDER BY t.created_at
            LIMIT 50
        """)).fetchall()

        cards = ""
        for r in rows:
            # r.lead_id exists from query
            cards += f"""
            <div style="background:#111827;padding:16px;margin:12px 0;border-radius:10px">
              <b>{r.task_type}</b><br>
              {r.full_name}<br>
              {r.phone}<br>
              <a href="/leads/{r.lead_id}">View Lead →</a>
            </div>
            """

        return HTMLResponse(f"""
        <html>
          <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
            <h2>Tasks</h2>
            <a href="/dashboard">← Back</a>
            {cards if cards else "<p>No tasks right now.</p>"}
          </body>
        </html>
        """)
    finally:
        db.close()
@app.get("/schedule", response_class=HTMLResponse)
def schedule():
    db = SessionLocal()
    try:
        tasks = (
            db.query(Task)
            .filter(Task.status == "PENDING")
            .order_by(Task.due_at.asc())
            .limit(30)
            .all()
        )

        rows = ""
        for t in tasks:
            rows += f"""
            <div style="background:#111827;padding:12px;margin:8px 0;border-radius:8px">
                <b>{t.type}</b><br>
                ⏰ {t.due_at or "ASAP"}<br>
                <a href="/leads/{t.lead_id}">View Lead</a>
            </div>
            """

        return HTMLResponse(f"""
        <html>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
            <h2>📅 Today’s AI Schedule</h2>
            {rows or "<p>No tasks planned.</p>"}
            <br><a href="/dashboard">← Dashboard</a>
        </body>
        </html>
        """)
    finally:
        db.close()
from fastapi_utils.tasks import repeat_every

@app.on_event("startup")
@repeat_every(seconds=300)  # every 5 minutes
def auto_ai_run():
    db = SessionLocal()
    try:
        run_ai_engine(db, Lead)
        db.commit()
    finally:
        db.close()
