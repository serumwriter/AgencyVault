# agencyvault_app/main.py
# ============================================================
# AgencyVault (FastAPI) — SAFE, SCALABLE, NO CRASHES
# - Works with thousands of leads (always LIMITs)
# - Schedule shows WHO + phone/email + notes + due time
# - AI planning creates actionable tasks (with notes/due_at)
# - Background executor sends TEXT tasks safely (rate-limited, retries)
# - No "Hi Bronze/Ethos" (smart greeting fallback)
# - Includes CSV upload back (simple /import/csv)
# ============================================================

import io
import os
import re
import csv
import json
import time
import threading
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from sqlalchemy import text

# PACKAGE-LOCAL IMPORTS (LOCKED)
from .database import SessionLocal, engine
from .models import Lead, LeadMemory
from .ai_employee import run_ai_engine
from .twilio_client import send_alert_sms, send_lead_sms
from .google_drive_import import import_google_sheet, import_drive_csv
from .image_import import extract_text_from_image, parse_leads_from_text

app = FastAPI(title="AgencyVault")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project/src


# ============================================================
# DB SCHEMA (idempotent)
# ============================================================
def ensure_tables():
    """Create/upgrade tables used by the planner/executor UI."""
    with engine.begin() as conn:
        # ai_tasks (planner -> schedule -> executor)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_tasks (
                id SERIAL PRIMARY KEY,
                task_type TEXT NOT NULL,
                lead_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'NEW',
                due_at TIMESTAMP NULL,
                notes TEXT NULL,
                attempt INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """))

        # Add missing columns safely (if table existed earlier)
        conn.execute(text("ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS due_at TIMESTAMP NULL;"))
        conn.execute(text("ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS notes TEXT NULL;"))
        conn.execute(text("ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS attempt INTEGER NOT NULL DEFAULT 0;"))
        conn.execute(text("ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS last_error TEXT NULL;"))
        conn.execute(text("ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT NOW();"))

        # ai_events (activity log shown on lead detail page)
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS ai_events (
                id SERIAL PRIMARY KEY,
                lead_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            );
        """))


def log_event(lead_id: int, event_type: str, message: str = ""):
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO ai_events (lead_id, event_type, message)
                VALUES (:lead_id, :event_type, :message)
            """),
            {"lead_id": lead_id, "event_type": event_type, "message": (message or "")[:2000]},
        )


def create_task(task_type: str, lead_id: int, notes: Optional[str] = None, due_at: Optional[datetime] = None):
    # Always ensure schema exists before inserting
    ensure_tables()
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO ai_tasks (task_type, lead_id, status, due_at, notes, attempt, created_at, updated_at)
                VALUES (:task_type, :lead_id, 'NEW', :due_at, :notes, 0, NOW(), NOW())
            """),
            {
                "task_type": task_type,
                "lead_id": lead_id,
                "due_at": due_at,
                "notes": (notes or None),
            },
        )


# ============================================================
# SANITIZATION / NORMALIZATION
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
# LeadMemory helpers (kept, but safe + cheap)
# ============================================================
def mem_get(db, lead_id, key):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def mem_set(db, lead_id, key, value):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = str(value)
        row.updated_at = datetime.utcnow()
    else:
        db.add(LeadMemory(
            lead_id=lead_id,
            key=key,
            value=str(value),
            updated_at=datetime.utcnow()
        ))

def needs_human(db, lead_id):
    return (mem_get(db, lead_id, "needs_human") or "0") == "1"


# ============================================================
# SMART GREETING (NO “BRONZE/ETHOS/LEAD”)
# ============================================================
BAD_NAME_WORDS = {
    "lead", "bronze", "silver", "gold", "facebook", "meta",
    "insurance", "prospect", "customer", "unknown", "test",
    "ethos", "policy", "quote", "client", "applicant"
}

def safe_greeting_name(full_name: str) -> Optional[str]:
    raw = (full_name or "").strip()
    if not raw:
        return None
    first = raw.split()[0].strip().lower()
    if (
        first in BAD_NAME_WORDS
        or len(first) < 2
        or any(c.isdigit() for c in first)
        or first.endswith("@")
    ):
        return None
    return raw.split()[0].capitalize()

def greeting_for_lead(lead: Lead) -> str:
    first = safe_greeting_name(getattr(lead, "full_name", "") or "")
    return f"Hi {first}" if first else "Hi there"


# ============================================================
# STATIC (PWA files)
# ============================================================
def safe_file(path: str) -> FileResponse:
    if not os.path.exists(path):
        return FileResponse(path, status_code=404)
    return FileResponse(path)

@app.get("/sw.js")
def service_worker():
    return safe_file(os.path.join(BASE_DIR, "app", "static", "sw.js"))

@app.get("/manifest.json")
def manifest():
    return safe_file(os.path.join(BASE_DIR, "app", "static", "manifest.json"))

@app.get("/static/icons/icon-192.png")
def icon_192():
    return safe_file(os.path.join(BASE_DIR, "app", "static", "icons", "icon-192.png"))


# ============================================================
# BASIC ROUTES
# ============================================================
@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/health")
def health():
    ensure_tables()
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}


# ============================================================
# IMPORT ROUTES (Google Drive, Image, CSV Upload)
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
            phone = normalize_phone(row.get("phone") or row.get("Phone") or row.get("mobile"))
            email = clean_text(row.get("email") or row.get("Email"))
            name = clean_text(row.get("name") or row.get("full_name") or row.get("Name")) or "Unknown"

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
        # image_import expects PIL Image in your earlier version;
        # your current image_import uses PIL + pytesseract directly from PIL Image,
        # but you previously passed BytesIO. To stay safe, we keep OCR in that module.
        text_data = extract_text_from_image(io.BytesIO(raw))  # your module supports this usage
        leads = parse_leads_from_text(text_data)

        added = 0
        for l in leads:
            phone = normalize_phone(l.get("phone"))
            email = clean_text(l.get("email"))
            name = clean_text(l.get("full_name") or l.get("name")) or "Unknown"

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

@app.post("/import/csv")
async def import_csv(file: UploadFile = File(...)):
    """
    Brings back simple lead upload.
    Expected columns: name/full_name, phone, email (case-insensitive).
    """
    db = SessionLocal()
    try:
        raw = (await file.read()).decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(raw))

        added = 0
        for row in reader:
            phone = normalize_phone(row.get("phone") or row.get("Phone") or row.get("mobile") or row.get("Mobile"))
            email = clean_text(row.get("email") or row.get("Email"))
            name = clean_text(row.get("name") or row.get("full_name") or row.get("Name") or row.get("Full Name")) or "Unknown"

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
# UI: DASHBOARD
# ============================================================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        ensure_tables()

        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(50).all()

        cards = ""
        for l in leads:
            hot = " 🔥" if needs_human(db, l.id) else ""
            cards += f"""
            <div style="background:#111827;padding:14px;margin:10px 0;border-radius:10px">
              <b>{(l.full_name or "Unknown")}{hot}</b><br>
              📞 {l.phone or "—"}<br>
              ✉️ {l.email or "—"}<br>
              <a href="/leads/{l.id}">View Lead →</a>
            </div>
            """

        # simple upload form (CSV)
        upload_box = """
        <div style="margin-top:18px;padding:14px;border:1px solid #233044;border-radius:10px;background:#0f1624">
          <h3 style="margin:0 0 10px 0;">Upload Leads (CSV)</h3>
          <form action="/import/csv" method="post" enctype="multipart/form-data">
            <input type="file" name="file" accept=".csv" />
            <button type="submit" style="margin-left:8px;">Upload</button>
          </form>
          <div style="opacity:0.75;margin-top:8px;font-size:13px;">Columns supported: name/full_name, phone, email</div>
        </div>
        """

        return HTMLResponse(f"""
        <html>
        <head><meta name="viewport" content="width=device-width, initial-scale=1" /></head>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
          <h1 style="margin:0 0 8px 0;">AgencyVault</h1>
          <div style="margin-bottom:14px;">
            <a href="/schedule">📅 Schedule</a>
            &nbsp;|&nbsp;
            <a href="/ai/run">🤖 Run AI Planner</a>
          </div>

          {upload_box}

          <h3 style="margin-top:18px;">Latest Leads</h3>
          {cards or "<p>No leads yet</p>"}
        </body>
        </html>
        """)
    finally:
        db.close()


# ============================================================
# UI: LEAD DETAIL (never crashes if tables exist)
# ============================================================
@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        ensure_tables()

        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Not found", status_code=404)

        mem = {
            m.key: m.value
            for m in db.query(LeadMemory).filter(LeadMemory.lead_id == lead.id).all()
        }

        tasks = db.execute(text("""
            SELECT id, task_type, status, due_at, notes, created_at
            FROM ai_tasks
            WHERE lead_id = :lead_id
            ORDER BY created_at DESC
            LIMIT 50
        """), {"lead_id": lead.id}).fetchall()

        events = db.execute(text("""
            SELECT event_type, message, created_at
            FROM ai_events
            WHERE lead_id = :lead_id
            ORDER BY created_at DESC
            LIMIT 50
        """), {"lead_id": lead.id}).fetchall()

        task_html = ""
        for t in tasks:
            task_html += (
                "<div style='padding:8px 0;border-bottom:1px solid #222'>"
                f"<b>{t.task_type}</b> <span style='opacity:0.8'>[{t.status}]</span>"
                f"<div style='opacity:0.8;font-size:13px'>Due: {t.due_at or 'now'} | Created: {t.created_at}</div>"
                f"<div style='opacity:0.9'>Notes: {t.notes or '-'}</div>"
                "</div>"
            )

        event_html = ""
        for e in events:
            event_html += (
                "<div style='padding:8px 0;border-bottom:1px solid #222'>"
                f"<b>{e.event_type}</b> <span style='opacity:0.8;font-size:13px'>({e.created_at})</span>"
                f"<div style='opacity:0.9'>{(e.message or '')}</div>"
                "</div>"
            )

        return HTMLResponse(f"""
        <html>
        <head><meta name="viewport" content="width=device-width, initial-scale=1" /></head>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
          <a href="/dashboard">← Back</a>
          <h2 style="margin:10px 0 6px 0;">{lead.full_name or "Unknown"}</h2>
          <div>📞 {lead.phone or "—"}</div>
          <div>✉️ {lead.email or "—"}</div>

          <h3 style="margin-top:18px;">AI Memory</h3>
          <pre style="background:#0f1624;padding:12px;border-radius:10px;overflow:auto">{json.dumps(mem, indent=2)}</pre>

          <h3 style="margin-top:18px;">Tasks</h3>
          <div style="background:#0f1624;padding:12px;border-radius:10px">{task_html or "<div>No tasks yet</div>"}</div>

          <h3 style="margin-top:18px;">Activity</h3>
          <div style="background:#0f1624;padding:12px;border-radius:10px">{event_html or "<div>No activity yet</div>"}</div>
        </body>
        </html>
        """)
    finally:
        db.close()


# ============================================================
# AI RUN (Planner-only; safe for thousands)
# ============================================================
@app.get("/ai/run")
def ai_run():
    db = SessionLocal()
    try:
        ensure_tables()

        # AI engine should only process small batches and return actions
        actions = run_ai_engine(db, Lead) or []

        planned = 0
        now = datetime.utcnow()

        for a in actions:
            task_type = (a.get("type") or "CALL").upper()
            lead_id = int(a["lead_id"])

            # Make tasks visible + useful
            confidence = a.get("confidence", "")
            evidence = a.get("evidence", "") or a.get("notes", "") or "AI planned action"
            due_at = a.get("due_at") or now

            notes = f"Conf: {confidence} | {evidence}".strip(" |")

            create_task(task_type, lead_id, notes=notes, due_at=due_at)
            log_event(lead_id, "TASK_PLANNED", f"{task_type} | {notes}")
            planned += 1

            if a.get("needs_human"):
                mem_set(db, lead_id, "needs_human", "1")

        db.commit()
        return {"planned": planned}
    finally:
        db.close()


# ============================================================
# SCHEDULE (FAST, SAFE, ALWAYS LIMITED)
# ============================================================
@app.get("/schedule", response_class=HTMLResponse)
def schedule():
    db = SessionLocal()
    try:
        ensure_tables()

        rows = db.execute(text("""
            SELECT
                t.id,
                t.task_type,
                t.lead_id,
                t.status,
                t.due_at,
                t.notes,
                t.attempt,
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
            <div style="padding:12px;margin:10px 0;border:1px solid #233044;border-radius:10px;background:#0f1624">
              <div style="display:flex;justify-content:space-between;gap:10px;">
                <div><b>{r.task_type}</b> <span style="opacity:0.8">[{r.status}]</span></div>
                <div style="opacity:0.75;font-size:13px">Due: {r.due_at or "now"} | Attempts: {r.attempt}</div>
              </div>
              <div style="margin-top:6px"><b>{r.full_name or "Unknown"}</b></div>
              <div style="opacity:0.9">📞 {r.phone or "-"}</div>
              <div style="opacity:0.9">✉️ {r.email or "-"}</div>
              <div style="margin-top:6px;opacity:0.9">Notes: {r.notes or "-"}</div>
              <div style="margin-top:8px">
                <a href="/leads/{r.lead_id}">Open lead</a>
              </div>
            </div>
            """

        return HTMLResponse(f"""
        <html>
        <head><meta name="viewport" content="width=device-width, initial-scale=1" /></head>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px">
          <a href="/dashboard">← Back</a>
          <h2 style="margin:10px 0 14px 0;">Schedule</h2>
          {html_rows or "<p>No tasks</p>"}
        </body>
        </html>
        """)
    finally:
        db.close()


# ============================================================
# BACKGROUND TASK EXECUTOR (TEXT only, throttled, retries)
# ============================================================
def _task_executor_loop():
    """
    Sends TEXT tasks to the lead phone.
    - Throttled to protect Twilio
    - Marks FAILED with error if Twilio throws
    - Never loops forever on bad leads
    """
    SEND_SLEEP_SECONDS = int(os.getenv("EXECUTOR_SLEEP_SECONDS", "90").strip() or "90")
    MAX_ATTEMPTS = int(os.getenv("EXECUTOR_MAX_ATTEMPTS", "3").strip() or "3")

    while True:
        try:
            ensure_tables()
            db = SessionLocal()
            try:
                # Only due tasks, only NEW, only TEXT
                tasks = db.execute(text("""
                    SELECT id, lead_id, notes, attempt
                    FROM ai_tasks
                    WHERE status='NEW'
                      AND task_type='TEXT'
                      AND (due_at IS NULL OR due_at <= NOW())
                    ORDER BY due_at NULLS FIRST, id ASC
                    LIMIT 5
                """)).fetchall()

                for t in tasks:
                    lead = db.query(Lead).filter_by(id=t.lead_id).first()

                    # Increment attempt early (so we see progress)
                    db.execute(text("""
                        UPDATE ai_tasks
                        SET attempt = attempt + 1,
                            updated_at = NOW()
                        WHERE id = :id
                    """), {"id": t.id})

                    if not lead or not lead.phone:
                        db.execute(text("""
                            UPDATE ai_tasks
                            SET status='DONE', last_error='missing lead/phone', updated_at=NOW()
                            WHERE id=:id
                        """), {"id": t.id})
                        continue

                    if (t.attempt or 0) >= MAX_ATTEMPTS:
                        db.execute(text("""
                            UPDATE ai_tasks
                            SET status='FAILED', last_error='max attempts', updated_at=NOW()
                            WHERE id=:id
                        """), {"id": t.id})
                        log_event(t.lead_id, "TEXT_FAILED", "Max attempts reached")
                        continue

                    greeting = greeting_for_lead(lead)

                    # Message (no junk names)
                    msg = (
                        f"{greeting}, this is a quick follow-up on your life insurance request. "
                        "I’ll be giving you a quick call shortly — no rush."
                    )

                    try:
                        send_lead_sms(lead.phone, msg)

                        db.execute(text("""
                            UPDATE ai_tasks
                            SET status='DONE', last_error=NULL, updated_at=NOW()
                            WHERE id=:id
                        """), {"id": t.id})

                        log_event(lead.id, "TEXT_SENT", msg)

                    except Exception as e:
                        err = str(e)[:500]
                        db.execute(text("""
                            UPDATE ai_tasks
                            SET last_error=:err,
                                due_at = NOW() + INTERVAL '10 minutes',
                                updated_at=NOW()
                            WHERE id=:id
                        """), {"id": t.id, "err": err})

                        log_event(lead.id, "TEXT_ERROR", err)

                db.commit()

            finally:
                db.close()

        except Exception as e:
            print("TASK EXECUTOR ERROR:", e)

        time.sleep(SEND_SLEEP_SECONDS)


@app.on_event("startup")
def startup():
    ensure_tables()

    if os.getenv("ENABLE_AUTORUN", "0").strip() == "1":
        threading.Thread(target=_task_executor_loop, daemon=True).start()
        print("Task executor started.")
@app.post("/twilio/recording")
async def twilio_recording_webhook(
    RecordingSid: str = Form(...),
    RecordingUrl: str = Form(...),
    CallSid: str = Form(...),
):
    db = SessionLocal()
    try:
        # Ask Twilio for transcript
        client = get_twilio_client()
        recordings = client.recordings(RecordingSid).fetch()

        if recordings.transcription_sid:
            transcript = client.transcriptions(
                recordings.transcription_sid
            ).fetch()

            # Save transcript
            db.execute(text("""
                INSERT INTO ai_events (lead_id, event_type, message)
                VALUES (
                    (SELECT lead_id FROM ai_tasks WHERE status='DONE' ORDER BY created_at DESC LIMIT 1),
                    'CALL_TRANSCRIPT',
                    :msg
                )
            """), {"msg": transcript.transcription_text})

            db.commit()

    except Exception as e:
        print("TRANSCRIPTION ERROR:", e)
    finally:
        db.close()

    return {"ok": True}
