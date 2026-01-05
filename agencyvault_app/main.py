# agencyvault_app/main.py

import io
import json
import os
import re
import time
import threading
from datetime import datetime, timedelta

from fastapi.responses import FileResponse
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

from ai_tasks import create_task, log_event

# ============================================================
# APP
# ============================================================
app = FastAPI(title="AgencyVault")

# ============================================================
# SANITIZATION
# ============================================================
def ensure_ai_events_table():
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_events (
                id SERIAL PRIMARY KEY,
                lead_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """))
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
@app.get("/sw.js")
def service_worker():
    return FileResponse("app/static/sw.js")

@app.get("/manifest.json")
def manifest():
    return FileResponse("app/static/manifest.json")

@app.get("/static/icons/icon-192.png")
def icon_192():
    return FileResponse("app/static/icons/icon-192.png")
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
        events = db.execute(text("""
            SELECT event_type, message, created_at
            FROM ai_events
            WHERE lead_id = :lead_id
            ORDER BY created_at DESC
            LIMIT 50
        """), {"lead_id": lead.id}).fetchall()

        event_html = ""
        for e in events:
            event_html += f"<div style='padding:6px 0;border-bottom:1px solid #222'>{e.created_at} - <b>{e.event_type}</b> - {e.message or ''}</div>"

        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;padding:20px">
        <h2>{lead.full_name}</h2>
        <p>{lead.phone}</p>
        <p>{lead.email or "—"}</p>
        <h3>AI Profile</h3>
        <pre>{json.dumps(mem, indent=2)}</pre>
        <a href="/dashboard">← Back</a>
        </body></html>
               <h3>Activity</h3>
        {event_html or "<p>No activity yet</p>"}   
    """)
    finally:
        db.close()

@app.get("/debug/task-leads")
def debug_task_leads():
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT lead_id, COUNT(*) AS cnt
            FROM ai_tasks
            GROUP BY lead_id
            ORDER BY lead_id DESC
            LIMIT 20
        """)).fetchall()
        return [{"lead_id": r.lead_id, "count": r.cnt} for r in rows]
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
        for a in actions:
            create_task(
                a["type"],
                a["lead_id"],
                notes=a.get("notes"),
                due_at=a.get("due_at"),
            )
            log_event(
                a["lead_id"],
                "TASK_PLANNED",
                f"{a['type']} planned"
            )
        return {"planned": len(actions)}
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
            SELECT
                t.id,
                t.task_type,
                t.lead_id,
                t.status,
                t.due_at,
                t.notes,
                l.full_name,
                l.phone,
                l.email
            FROM ai_tasks t
            JOIN leads l ON l.id = t.lead_id
            ORDER BY
                CASE WHEN t.status='NEW' THEN 0 ELSE 1 END,
                t.due_at NULLS FIRST,
                t.id DESC
            LIMIT 300
        """)).fetchall()

        html_rows = ""
        for r in rows:
            html_rows += f"""
            <div style="padding:12px;margin:10px 0;border:1px solid #333;border-radius:10px;background:#0f1624">
              <b>{r.task_type}</b> <span style="opacity:0.8">[{r.status}]</span><br>
              {r.full_name or "Unknown"}<br>
              Phone: {r.phone}<br>
              Email: {r.email or "-"}<br>
              Notes: {r.notes or "-"}<br>
              Due: {r.due_at or "now"}<br>
              <a href="/leads/{r.lead_id}">Open lead</a>
            </div>
            """

        return HTMLResponse(f"""
        <html>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
          <h2>Schedule</h2>
          {html_rows or "<p>No tasks</p>"}
        </body>
        </html>
        """)
    finally:
        db.close()

# ============================================================
# BACKGROUND TASK EXECUTOR (SAFE)
# ============================================================
# ============================================================
# BACKGROUND TASK EXECUTOR
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
                    ORDER BY created_at ASC
                    LIMIT 10
                """)).fetchall()

                for r in rows:
                    lead = db.query(Lead).filter_by(id=r.lead_id).first()
                    if not lead or not lead.phone:
                        db.execute(
                            text("UPDATE ai_tasks SET status='DONE' WHERE id=:id"),
                            {"id": r.id},
                        )
                        continue

                    message = (
                        f"Hi {lead.full_name.split()[0]}, "
                        "this is a quick follow-up about your life insurance request. "
                        "I’ll be calling you shortly."
                    )

                    send_lead_sms(lead.phone, message)

                    db.execute(
                        text("UPDATE ai_tasks SET status='DONE' WHERE id=:id"),
                        {"id": r.id},
                    )

                db.commit()

            finally:
                db.close()

        except Exception as e:
            print("TASK EXECUTOR ERROR:", e)

        time.sleep(90)  # throttle = safe for Twilio


@app.on_event("startup")
def startup():
    # Ensure AI event log table exists
    ensure_ai_events_table()

    # Start executor if enabled
    if os.getenv("ENABLE_AUTORUN") == "1":
        threading.Thread(target=_task_executor_loop, daemon=True).start()
        print("Task executor started.")

