# agencyvault_app/main.py

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from sqlalchemy import text
from datetime import datetime, timedelta
import csv
import os
import re

# PACKAGE-LOCAL IMPORTS (LOCKED)
from .database import SessionLocal, engine
from .models import Lead, LeadMemory, Task
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms
from ai_tasks import create_task
from agencyvault_app.google_drive_import import (
    import_google_sheet,
    import_drive_csv,
)
import pandas as pd

app = FastAPI(title="AgencyVault")

# ============================================================
# SANITIZATION
# ============================================================
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def clean_text(val):
    if val is None:
        return None
    return _CONTROL_RE.sub("", str(val)).replace("\x00", "").strip() or None

def normalize_phone(s):
    s = clean_text(s) or ""
    d = re.sub(r"\D", "", s)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None

def dedupe_exists(db, phone, email):
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

# ============================================================
# LeadMemory helpers
# ============================================================
def mem_get(db, lead_id, key):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def mem_set(db, lead_id, key, value):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(LeadMemory(
            lead_id=lead_id,
            key=key,
            value=value,
            updated_at=datetime.utcnow()
        ))

def needs_human(db, lead_id):
    return (mem_get(db, lead_id, "needs_human") or "0") == "1"
def learn(db, lead_id, key, value):
    if value:
        mem_set(db, lead_id, key, value)

# ============================================================
# BASIC ROUTES
# ============================================================
@app.post("/import/google-drive")
def import_from_google_drive(
    file_id: str = Form(...),
    file_type: str = Form(...),  # "sheet" or "csv"
    creds_json: str = Form(...)
):
    db = SessionLocal()
    try:
        creds = json.loads(creds_json)

        if file_type == "sheet":
            df = import_google_sheet(creds, file_id)
        else:
            df = import_drive_csv(creds, file_id)

        added = 0

        for _, row in df.iterrows():
            phone = normalize_phone(str(row.get("phone", "")))
            email = clean_text(row.get("email"))
            name = clean_text(row.get("name")) or "Unknown"

            if not phone or dedupe_exists(db, phone, email):
                continue

            db.add(Lead(
                full_name=name,
                phone=phone,
                email=email,
                state="NEW",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ))
            added += 1

        db.commit()
        return {"imported": added}

    finally:
        db.close()

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
    try:
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
        cards = ""

        for l in leads:
            badge = " 🔥" if needs_human(db, l.id) else ""
            cards += f"""
            <div style="background:#111827;padding:14px;margin:10px 0;border-radius:10px">
              <b>{l.full_name}{badge}</b><br>
              📞 {l.phone}<br>
              ✉️ {l.email or "—"}<br>
              <a href="/leads/{l.id}">View Lead →</a>
            </div>
            """

        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
        <h1>AgencyVault</h1>
        <a href="/schedule">📅 Schedule</a> | <a href="/ai/run">Run AI</a>
        <h3>Leads</h3>
        {cards or "<p>No leads</p>"}
        </body></html>
        """)
    finally:
        db.close()

# ============================================================
# LEAD DETAIL (FIXED)
# ============================================================
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Not found", status_code=404)

        mem = {
            m.key: m.value
            for m in db.query(LeadMemory)
                .filter(LeadMemory.lead_id == lead.id)
                .all()
        }

        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
        <h2>{lead.full_name}</h2>
        <p>📞 {lead.phone}</p>
        <p>✉️ {lead.email or "—"}</p>

        <h3>AI Lead Profile</h3>
        <ul>
          <li>State: {mem.get("state","—")}</li>
          <li>Smoker: {mem.get("smoker","—")}</li>
          <li>Medical: {mem.get("medical","—")}</li>
          <li>Income: {mem.get("income","—")}</li>
          <li>Product Interest: {lead.product_interest or "—"}</li>
        </ul>

        <form method="post" action="/leads/{lead.id}/escalate">
          <button>Escalate</button>
        </form>

        <a href="/dashboard">← Back</a>
        </body></html>
        """)
    finally:
        db.close()

# ============================================================
# AI RUN
# ============================================================
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    try:
        actions = run_ai_engine(db, Lead) or []
        for a in actions:
            create_task(a["type"], a["lead_id"])
            if a.get("needs_human"):
                mem_set(db, a["lead_id"], "needs_human", "1")
        db.commit()
        return {"planned": len(actions)}
    finally:
        db.close()

# ============================================================
# SCHEDULE
# ============================================================
@app.get("/schedule", response_class=HTMLResponse)
def schedule():
    db = SessionLocal()
    try:
        tasks = db.query(Task).order_by(Task.due_at).limit(30).all()
        rows = "".join(
            f"<div>{t.type} → <a href='/leads/{t.lead_id}'>Lead</a></div>"
            for t in tasks
        )
        return HTMLResponse(f"<html><body>{rows or 'No tasks'}</body></html>")
    finally:
        db.close()
# ============================================================
# TASK EXECUTOR (TEXT auto-send)
# ============================================================
def _task_executor_loop():
    while True:
        try:
            db = SessionLocal()
            try:
                # Fetch NEW text tasks that are due
                rows = db.execute(text("""
                    SELECT t.id, t.task_type, t.lead_id, t.notes
                    FROM ai_tasks t
                    WHERE t.status='NEW'
                      AND t.task_type='TEXT'
                      AND (t.due_at IS NULL OR t.due_at <= NOW())
                    ORDER BY t.created_at
                    LIMIT 10
                """)).fetchall()

                for r in rows:
                    lead = db.query(Lead).filter(Lead.id == r.lead_id).first()
                    if not lead or not lead.phone:
                        db.execute(text(
                            "UPDATE ai_tasks SET status='FAILED' WHERE id=:id"
                        ), {"id": r.id})
                        continue

                    # Send SMS
                    send_alert_sms(r.notes or f"Hi {lead.full_name}, just following up.")

                    # Mark task done
                    db.execute(text(
                        "UPDATE ai_tasks SET status='DONE' WHERE id=:id"
                    ), {"id": r.id})

                db.commit()
            finally:
                db.close()
        except Exception as e:
            print("TASK EXECUTOR ERROR:", repr(e))

        time.sleep(60)  # every 60 seconds


@app.on_event("startup")
def startup_task_executor():
    if os.getenv("ENABLE_AUTORUN", "0") != "1":
        return
    t = threading.Thread(target=_task_executor_loop, daemon=True)
    t.start()
    print("Task executor started (TEXT auto-send).")
