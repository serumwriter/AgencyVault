# agencyvault_app/main.py

import io
import json
import os
import re
import time
import threading
from datetime import datetime, timedelta

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy import text

# ============================================================
# PACKAGE-LOCAL IMPORTS (LOCKED)
# ============================================================
from .database import SessionLocal, engine
from .models import Lead, LeadMemory
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms
from .google_drive_import import import_google_sheet, import_drive_csv
from .image_import import extract_text_from_image, parse_leads_from_text

from ai_tasks import create_task

# ============================================================
# APP
# ============================================================
app = FastAPI(title="AgencyVault")

# ============================================================
# SANITIZATION
# ============================================================
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

def clean_text(val):
    if val is None:
        return None
    return _CONTROL_RE.sub("", str(val)).replace("\x00", "").strip() or None

def normalize_phone(val):
    val = clean_text(val) or ""
    d = re.sub(r"\D", "", val)
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

# ============================================================
# IMPORT ROUTES
# ============================================================
@app.post("/import/google-drive")
def import_from_google_drive(
    file_id: str = Form(...),
    file_type: str = Form(...),  # sheet | csv
    creds_json: str = Form(...)
):
    db = SessionLocal()
    try:
        creds = json.loads(creds_json)

        if file_type == "sheet":
            df = import_google_sheet(creds, file_id)
        elif file_type == "csv":
            df = import_drive_csv(creds, file_id)
        else:
            return {"error": "file_type must be sheet or csv"}

        added = 0
        for _, row in df.iterrows():
            phone = normalize_phone(row.get("phone"))
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

@app.post("/import/image")
async def import_from_image(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        raw = await file.read()
        text_data = extract_text_from_image(io.BytesIO(raw))
        leads = parse_leads_from_text(text_data)

        added = 0
        for l in leads:
            phone = normalize_phone(l.get("phone"))
            email = clean_text(l.get("email"))
            name = clean_text(l.get("name")) or "Unknown"

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
    try:
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()
        cards = ""

        for l in leads:
            hot = " 🔥" if needs_human(db, l.id) else ""
            cards += f"""
            <div style="background:#111827;padding:14px;margin:10px 0;border-radius:10px">
              <b>{l.full_name}{hot}</b><br>
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
        {cards or "<p>No leads yet</p>"}
        </body></html>
        """)
    finally:
        db.close()

# ============================================================
# LEAD DETAIL
# ============================================================
@app.get("/leads/{{lead_id}}", response_class=HTMLResponse)
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
        <html><body style="background:#0b0f17;color:#e6edf3;padding:20px">
        <h2>{lead.full_name}</h2>
        <p>{lead.phone}</p>
        <p>{lead.email or "—"}</p>
        <h3>AI Profile</h3>
        <pre>{json.dumps(mem, indent=2)}</pre>
        <a href="/dashboard">← Back</a>
        </body></html>
        """)
    finally:
        db.close()

# ============================================================
# AI RUN (PLANNER ONLY — SAFE FOR THOUSANDS)
# ============================================================
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    try:
        actions = run_ai_engine(db, Lead) or []
        created = 0

        for a in actions:
            create_task(a["type"], a["lead_id"])
            created += 1
            if a.get("needs_human"):
                mem_set(db, a["lead_id"], "needs_human", "1")

        db.commit()
        return {"planned_tasks": created}
    finally:
        db.close()

# ============================================================
# SCHEDULE (SAFE, NO ORM, NO CRASH)
# ============================================================
@app.get("/schedule", response_class=HTMLResponse)
def schedule():
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id, task_type, lead_id, status, created_at
            FROM ai_tasks
            ORDER BY created_at DESC
            LIMIT 100
        """)).fetchall()

        html = "<h2>📅 Task Schedule</h2>"

        if not rows:
            html += "<p>No tasks yet</p>"
        else:
            html += "<ul>"
            for r in rows:
                html += (
                    "<li>"
                    f"<b>{r.task_type}</b> | "
                    f"<a href='/leads/{r.lead_id}'>Lead #{r.lead_id}</a> | "
                    f"{r.status} | "
                    f"{r.created_at}"
                    "</li>"
                )
            html += "</ul>"

        html += "<br><a href='/dashboard'>← Back</a>"
        return HTMLResponse(f"<html><body>{html}</body></html>")
    finally:
        db.close()

# ============================================================
# BACKGROUND TASK EXECUTOR (SAFE)
# ============================================================
def _task_executor_loop():
    while True:
        try:
            db = SessionLocal()
            try:
                rows = db.execute(text("""
                    SELECT id, task_type, lead_id
                    FROM ai_tasks
                    WHERE status='NEW'
                      AND task_type='TEXT'
                    LIMIT 25
                """)).fetchall()

                for r in rows:
                    lead = db.query(Lead).filter_by(id=r.lead_id).first()
                    if not lead or not lead.phone:
                        db.execute(
                            text("UPDATE ai_tasks SET status='DONE' WHERE id=:id"),
                            {"id": r.id},
                        )
                        continue

                    send_alert_sms(
                        f"Hi {lead.full_name.split()[0]}, just following up on your request."
                    )

                    db.execute(
                        text("UPDATE ai_tasks SET status='DONE' WHERE id=:id"),
                        {"id": r.id},
                    )

                db.commit()
            finally:
                db.close()

        except Exception as e:
            print("TASK EXECUTOR ERROR:", e)

        time.sleep(60)

@app.on_event("startup")
def startup_task_executor():
    if os.getenv("ENABLE_AUTORUN") == "1":
        threading.Thread(target=_task_executor_loop, daemon=True).start()
        print("Task executor started.")
