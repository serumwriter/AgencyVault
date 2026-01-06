# ============================================================
# AgencyVault — Command Center (Enterprise Hardened)
# ============================================================

import csv
import io
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import text

from .database import engine, SessionLocal
from .models import (
    Base,
    Lead,
    Action,
    AgentRun,
    LeadMemory,
    AuditLog,
    Message,
)
from .ai_employee import plan_actions
from .google_drive_import import (
    import_google_sheet,
    import_drive_csv,
    import_google_doc_text,
)
from .image_import import extract_text_from_image_bytes, parse_leads_from_text
from .twilio_client import lead_id_for_call_sid, send_alert_sms

app = FastAPI(title="AgencyVault")

# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

# ============================================================
# UTILITIES (SAFE, CENTRALIZED)
# ============================================================

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def now():
    return datetime.utcnow()

def clean_text(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    return _CONTROL_RE.sub("", str(val)).replace("\x00", "").strip() or None

def normalize_phone(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    digits = re.sub(r"\D", "", val)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return None

def dedupe_exists(db: Session, phone: Optional[str], email: Optional[str]) -> bool:
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

def log(db: Session, event: str, detail: str = "", lead_id: int | None = None, run_id: int | None = None):
    db.add(AuditLog(
        lead_id=lead_id,
        run_id=run_id,
        event=event,
        detail=(detail or "")[:5000],
    ))

def set_memory(db: Session, lead_id: int, key: str, value: str):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = now()
    else:
        db.add(LeadMemory(
            lead_id=lead_id,
            key=key,
            value=value,
            updated_at=now(),
        ))

def cancel_pending_actions(db: Session, lead_id: int, reason: str):
    actions = (
        db.query(Action)
        .filter(Action.lead_id == lead_id, Action.status == "PENDING")
        .all()
    )
    for a in actions:
        a.status = "SKIPPED"
        a.error = f"Canceled: {reason}"
        a.finished_at = now()
    log(db, "ACTIONS_CANCELED", reason, lead_id=lead_id)

# ============================================================
# HEALTH / ROOT
# ============================================================

@app.get("/health")
def health():
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

# ============================================================
# DASHBOARD (COMMAND CENTER)
# ============================================================

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        total = db.query(Lead).count()
        new = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()
        pending = db.query(Action).filter(Action.status == "PENDING").count()

        recent_logs = (
            db.query(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(12)
            .all()
        )

        since = now() - timedelta(hours=24)
        hot = (
            db.query(Lead)
            .join(Message, Message.lead_id == Lead.id)
            .filter(
                Message.direction == "IN",
                Message.created_at >= since,
                Lead.state != "DO_NOT_CONTACT",
            )
            .order_by(Message.created_at.desc())
            .limit(8)
            .all()
        )

        leads = (
            db.query(Lead)
            .order_by(Lead.created_at.desc())
            .limit(20)
            .all()
        )

        # ---- Render HTML blocks cleanly ----

        hot_html = "".join(
            f"""
            <div class="card hot">
              <b>{l.full_name}</b> [{l.state}]<br>
              📞 {l.phone} &nbsp; ✉️ {l.email or "—"}
              <div><a class="btn" href="/leads/{l.id}">Open</a></div>
            </div>
            """
            for l in hot
        )

        leads_html = "".join(
            f"""
            <div class="card">
              <b>{l.full_name}</b>
              <span class="pill">{l.state}</span><br>
              📞 {l.phone} &nbsp; ✉️ {l.email or "—"}
              <div style="margin-top:8px;">
                <a class="btn" href="/leads/{l.id}">Open</a>
                <form method="post" action="/leads/{l.id}/delete"
                      style="display:inline"
                      onsubmit="return confirm('Delete this lead permanently?');">
                  <button class="btn danger">Delete</button>
                </form>
              </div>
            </div>
            """
            for l in leads
        )

        logs_html = "".join(
            f"""
            <div class="log">
              <b>{x.event}</b> lead={x.lead_id} • {x.created_at}<br>
              <span class="muted">{x.detail[:400]}</span>
            </div>
            """
            for x in recent_logs
        )

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgencyVault</title>
<style>
body {{ background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px }}
.btn {{ background:#111827;border:1px solid #223047;color:#e6edf3;padding:6px 10px;border-radius:8px }}
.danger {{ background:#2a0f14;border-color:#5b1a22 }}
.card {{ background:#0b1220;border:1px solid #1f2b3e;border-radius:12px;padding:12px;margin:10px 0 }}
.card.hot {{ border-color:#5b4b14;background:#141003 }}
.pill {{ font-size:12px;padding:2px 8px;border-radius:999px;border:1px solid #223047 }}
.log {{ border-bottom:1px solid #1f2b3e;padding:8px 0 }}
.muted {{ opacity:.7;font-size:13px }}
</style>
</head>
<body>

<h1>AgencyVault — Command Center</h1>

<p>
Total: <b>{total}</b> |
NEW: <b>{new}</b> |
WORKING: <b>{working}</b> |
DNC: <b>{dnc}</b> |
Pending Actions: <b>{pending}</b>
</p>

<h2>🔥 Hot Leads</h2>
{hot_html or "<div class='muted'>No hot replies</div>"}

<h2>📇 Recent Leads</h2>
{leads_html or "<div class='muted'>No leads</div>"}

<h2>🧾 Activity</h2>
{logs_html or "<div class='muted'>No activity</div>"}

<p>
<a class="btn" href="/imports">Imports</a>
<a class="btn" href="/actions">Action Queue</a>
<a class="btn" href="/ai/plan">Run AI Planner</a>
</p>

</body>
</html>
""")
    finally:
        db.close()

# ============================================================
# LEADS CRUD
# ============================================================

@app.get("/leads/new", response_class=HTMLResponse)
def leads_new_form():
    return HTMLResponse("""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
    <h2>New Lead</h2>
    <form method="post">
      Name<br><input name="full_name"><br><br>
      Phone<br><input name="phone"><br><br>
      Email<br><input name="email"><br><br>
      <button>Create</button>
    </form>
    </body></html>
    """)

@app.post("/leads/new")
def leads_new(full_name: str = Form(""), phone: str = Form(""), email: str = Form("")):
    db = SessionLocal()
    try:
        p = normalize_phone(phone)
        if not p:
            return JSONResponse({"error": "invalid phone"}, 400)
        e = clean_text(email)
        n = clean_text(full_name) or "Unknown"
        if dedupe_exists(db, p, e):
            return JSONResponse({"error": "duplicate"}, 409)

        db.add(Lead(
            full_name=n,
            phone=p,
            email=e,
            state="NEW",
            created_at=now(),
            updated_at=now(),
        ))
        log(db, "LEAD_CREATED", f"{n} {p}")
        db.commit()
        return RedirectResponse("/dashboard", 303)
    finally:
        db.close()

@app.post("/leads/{lead_id}/delete")
def delete_lead(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if lead:
            log(db, "LEAD_DELETED", f"{lead.full_name} {lead.phone}", lead_id=lead.id)
            cancel_pending_actions(db, lead.id, "Manual delete")
            db.delete(lead)
            db.commit()
        return RedirectResponse("/dashboard", 303)
    finally:
        db.close()

# ============================================================
# AI PLANNER
# ============================================================

@app.get("/ai/plan")
def ai_plan():
    db = SessionLocal()
    try:
        out = plan_actions(db, batch_size=25)
        log(db, "AI_PLAN_TRIGGERED", json.dumps(out), run_id=out.get("run_id"))
        db.commit()
        return out
    finally:
        db.close()

# ============================================================
# IMPORTS (CSV, IMAGE, GOOGLE)
# ============================================================

def import_row(db: Session, row: dict) -> bool:
    phone = normalize_phone(row.get("phone") or row.get("Phone"))
    if not phone:
        return False
    email = clean_text(row.get("email"))
    name = clean_text(row.get("name") or row.get("full_name")) or "Unknown"
    if dedupe_exists(db, phone, email):
        return False
    db.add(Lead(
        full_name=name,
        phone=phone,
        email=email,
        state="NEW",
        created_at=now(),
        updated_at=now(),
    ))
    return True

@app.post("/import/csv")
async def import_csv(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        raw = (await file.read()).decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(raw))
        added = sum(1 for r in reader if import_row(db, r))
        log(db, "IMPORT_CSV", f"added={added}")
        db.commit()
        return {"imported": added}
    finally:
        db.close()

# ============================================================
# ADMIN — MASS DELETE
# ============================================================

@app.get("/admin/delete-all-leads", response_class=HTMLResponse)
def admin_delete_page():
    return HTMLResponse("""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:40px">
    <h2>⚠️ Delete ALL Leads</h2>
    <form method="post">
      Type <b>DELETE ALL LEADS</b><br>
      <input name="confirm"><br><br>
      <button style="background:#b91c1c;color:white">Delete Everything</button>
    </form>
    </body></html>
    """)

@app.post("/admin/delete-all-leads")
def admin_delete(confirm: str = Form(...)):
    if confirm.strip() != "DELETE ALL LEADS":
        return {"error": "confirmation mismatch"}
    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM actions"))
        db.execute(text("DELETE FROM lead_memory"))
        db.execute(text("DELETE FROM leads"))
        db.commit()
        return {"ok": True}
    finally:
        db.close()
