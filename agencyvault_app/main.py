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

# ============================================================
# APP
# ============================================================
app = FastAPI(title="AgencyVault")

# ============================================================
# HARD TEXT CLEANING (FIXES POSTGRES NUL BYTE CRASHES)
# ============================================================
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def clean_text(val):
    if not val:
        return None
    if not isinstance(val, str):
        val = str(val)
    return CONTROL_RE.sub("", val).strip() or None

def normalize_phone(val):
    val = clean_text(val)
    if not val:
        return None
    digits = re.sub(r"\D", "", val)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return val

def looks_like_phone(val):
    val = clean_text(val) or ""
    digits = re.sub(r"\D", "", val)
    return len(digits) in (10, 11)

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
# STYLES
# ============================================================
def base_styles():
    return """
    <style>
      body{background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px}
      h1{font-size:32px;margin:0}
      .sub{opacity:.85;margin-bottom:18px}
      .row{display:flex;gap:12px;flex-wrap:wrap}
      .box{background:#020617;padding:18px;border-radius:12px;flex:1;min-width:280px}
      .card{background:#111827;padding:16px;margin:12px 0;border-radius:10px}
      input,textarea,button{padding:10px;margin:6px 0;width:100%;border-radius:8px;border:none}
      button{background:#2563eb;color:white;font-weight:800;cursor:pointer}
      .danger{background:#dc2626}
      .warn{background:#f59e0b;color:black;font-weight:900}
      a{color:#60a5fa;text-decoration:none}
      .pill{display:inline-block;padding:4px 10px;border-radius:999px;background:#0f172a;margin-left:8px;font-size:12px}
      .muted{opacity:.8}
    </style>
    """

# ============================================================
# PWA (STOPS /sw.js ERRORS)
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
        content="self.addEventListener('fetch', function(event) {});",
        media_type="application/javascript"
    )

# ============================================================
# ROUTES
# ============================================================
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()

    total = db.execute(text("SELECT COUNT(*) FROM leads")).scalar() or 0
    hot = db.execute(text("SELECT COUNT(*) FROM leads WHERE needs_human=1 AND do_not_contact=0")).scalar() or 0
    dups = db.execute(text("SELECT COUNT(*) FROM leads WHERE status='DUPLICATE'")).scalar() or 0
    db.close()

    cards = ""
    for l in leads:
        pill = ""
        if getattr(l, "do_not_contact", 0):
            pill = "<span class='pill'>DNC</span>"
        elif getattr(l, "needs_human", 0):
            pill = "<span class='pill' style='background:#7f1d1d'>HOT</span>"
        elif (l.product_interest or "").upper() in ("IUL", "ANNUITY"):
            pill = f"<span class='pill'>{l.product_interest}</span>"

        cards += f"""
        <div class="card">
          <b>{l.full_name or "Unnamed Lead"}{pill}</b><br>
          📞 {l.phone or "—"}<br>
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
          <h3>➕ Manual Add</h3>
          <form method="post" action="/leads/manual">
            <input name="full_name" placeholder="Full Name">
            <input name="phone" placeholder="Phone" required>
            <input name="email" placeholder="Email">
            <input name="source" placeholder="Source">
            <button>Add Lead</button>
          </form>
        </div>

        <div class="box">
          <h3>📤 Upload CSV</h3>
          <form method="post" action="/leads/upload" enctype="multipart/form-data">
            <input type="file" name="file" required>
            <button>Upload</button>
          </form>
          <div class="muted">Auto-dedupe enabled</div>
        </div>

        <div class="box">
          <h3>📞 Live Ops</h3>
          <div>Total Leads: <b>{total}</b></div>
          <div>Needs You: <b>{hot}</b></div>
          <div>Duplicates: <b>{dups}</b></div>
          <a href="/tasks">Tasks →</a><br>
          <a href="/ai/run">Run AI →</a>
        </div>
      </div>

      <h3>Recent Leads</h3>
      {cards}
    </body>
    </html>
    """)

# ============================================================
# LEAD CREATION
# ============================================================
@app.post("/leads/manual")
def add_lead_manual(
    full_name: str = Form(""),
    phone: str = Form(...),
    email: str = Form(""),
    source: str = Form("")
):
    db = SessionLocal()

    p = normalize_phone(phone)
    e = clean_text(email)

    if dedupe_exists(db, p, e):
        db.close()
        return RedirectResponse("/dashboard", status_code=303)

    db.add(Lead(
        full_name=clean_text(full_name) or "Unknown",
        phone=p,
        email=e,
        source=clean_text(source),
        status="New",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    ))
    db.commit()
    db.close()
    return RedirectResponse("/dashboard", status_code=303)

@app.post("/leads/upload")
def upload(file: UploadFile = File(...)):
    raw = file.file.read().decode("utf-8", errors="ignore").replace("\x00", "")
    rows = csv.reader(raw.splitlines())

    db = SessionLocal()
    added = skipped = 0

    for r in rows:
        vals = [clean_text(c) for c in r if clean_text(c)]
        name = next((v for v in vals if looks_like_name(v)), None)
        phone = next((v for v in vals if looks_like_phone(v)), None)
        email = next((v for v in vals if "@" in v), None)

        if not phone:
            continue

        p = normalize_phone(phone)
        e = clean_text(email)

        if dedupe_exists(db, p, e):
            skipped += 1
            continue

        db.add(Lead(
            full_name=name or "Unknown",
            phone=p,
            email=e,
            status="New",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ))
        added += 1

    db.commit()
    db.close()

    return HTMLResponse(
        f"<h3>Imported {added}</h3><p>Skipped duplicates: {skipped}</p><a href='/dashboard'>Back</a>"
    )

# ============================================================
# LEAD DETAIL
# ============================================================
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    db.close()

    if not lead:
        return HTMLResponse("Lead not found", status_code=404)

    return HTMLResponse(f"""
    <html>
    <head>{base_styles()}</head>
    <body>
      <a href="/dashboard">← Dashboard</a> | <a href="/tasks">Tasks</a>

      <div class="card">
        <h2>{lead.full_name}</h2>
        📞 {lead.phone}<br>
        ✉️ {lead.email or "—"}<br>
        <b>Status:</b> {lead.status}<br>
        <b>AI:</b> {lead.ai_confidence or "—"}/100<br>
        <b>Evidence:</b> {lead.ai_evidence or "—"}
      </div>

      <form method="post" action="/leads/{lead.id}/call"><button>📞 Call (Dry)</button></form>
      <form method="post" action="/leads/{lead.id}/escalate/now"><button class="danger">🔥 Escalate NOW</button></form>
      <form method="post" action="/leads/{lead.id}/escalate/problem"><button class="warn">⚠️ Escalate Problem</button></form>
    </body>
    </html>
    """)

# ============================================================
# ACTIONS
# ============================================================
@app.post("/leads/{lead_id}/call")
def call_lead(lead_id: int):
    create_task("CALL", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/escalate/now")
def escalate_now(lead_id: int):
    db = SessionLocal()
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if lead:
        lead.needs_human = 1
        lead.ai_confidence = max(lead.ai_confidence or 0, 95)
        lead.ai_evidence = (lead.ai_evidence or "") + "; wants coverage now"
        db.commit()

        create_task("ESCALATE_NOW", lead_id)
        send_alert_sms(f"🔥 HOT LEAD\n{lead.full_name}\n{lead.phone}")
    db.close()
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

@app.post("/leads/{lead_id}/escalate/problem")
def escalate_problem(lead_id: int):
    create_task("ESCALATE_PROBLEM", lead_id)
    return RedirectResponse(f"/leads/{lead_id}", status_code=303)

# ============================================================
# AI
# ============================================================
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    actions = run_ai_engine(db, Lead)
    for a in actions:
        create_task(a["type"], a["lead_id"])
    db.close()
    return {"planned": len(actions)}

# ============================================================
# TASKS
# ============================================================
@app.get("/tasks", response_class=HTMLResponse)
def tasks():
    db = SessionLocal()
    rows = db.execute(text("""
        SELECT t.id, t.task_type, l.full_name, l.phone, t.lead_id
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
        <div class="card">
          <b>{r.task_type}</b><br>
          {r.full_name}<br>
          {r.phone}<br>
          <a href="/leads/{r.lead_id}">View Lead →</a>
        </div>
        """

    return HTMLResponse(f"<html><head>{base_styles()}</head><body><h2>Tasks</h2>{cards}<a href='/dashboard'>← Back</a></body></html>")


