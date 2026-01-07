import csv
import io
import json
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from .database import engine, SessionLocal
from .models import Base, Lead, Action, AgentRun, LeadMemory, AuditLog, Message

# Optional Google imports (if you already have google_drive_import.py)
try:
    from .google_drive_import import import_google_sheet, import_drive_csv, import_google_doc_text
    GOOGLE_IMPORTS_OK = True
except Exception:
    GOOGLE_IMPORTS_OK = False

# Twilio functions (handle multiple possible names)
from .twilio_client import send_alert_sms, send_lead_sms

try:
    from .twilio_client import make_call_with_recording as _make_call
except Exception:
    try:
        from .twilio_client import make_call as _make_call
    except Exception:
        _make_call = None


# Optional PDF extraction
try:
    from pypdf import PdfReader
    PDF_OK = True
except Exception:
    PDF_OK = False

# Optional OCR
try:
    from PIL import Image
    import pytesseract
    OCR_OK = True
except Exception:
    OCR_OK = False


app = FastAPI(title="AgencyVault - Command Center")


# =========================
# Startup / Schema
# =========================
@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)


# =========================
# Normalization / Sanitization
# =========================
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_US_STATE_RE = re.compile(r"^(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)$", re.I)

BAD_NAME_WORDS = {
    "lead", "bronze", "silver", "gold", "ethos",
    "facebook", "insurance", "prospect", "unknown", "test",
    "meta", "client", "customer", "applicant"
}

def _now() -> datetime:
    return datetime.utcnow()

def clean_text(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = _CONTROL_RE.sub("", str(val)).replace("\x00", "").strip()
    return s or None

def normalize_phone(val: Any) -> Optional[str]:
    s = clean_text(val) or ""
    digits = re.sub(r"\D", "", s)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if s.startswith("+") and len(digits) >= 11:
        return "+" + digits
    return None

def safe_first_name(full_name: Optional[str]) -> str:
    if not full_name:
        return ""
    first = (full_name.strip().split() or [""])[0].strip().lower()
    if not first or first in BAD_NAME_WORDS or len(first) < 2:
        return ""
    if any(c.isdigit() for c in first):
        return ""
    return first.capitalize()

def normalize_state(val: Any) -> Optional[str]:
    s = (clean_text(val) or "").strip()
    if not s:
        return None
    s2 = s.upper()
    if _US_STATE_RE.match(s2):
        return s2
    return None

def dedupe_exists(db: Session, phone: Optional[str], email: Optional[str]) -> bool:
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False

def _log(db: Session, lead_id: Optional[int], run_id: Optional[int], event: str, detail: str):
    db.add(AuditLog(
        lead_id=lead_id,
        run_id=run_id,
        event=event,
        detail=(detail or "")[:5000],
        created_at=_now(),
    ))

def mem_set(db: Session, lead_id: int, key: str, value: str):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = _now()
    else:
        db.add(LeadMemory(lead_id=lead_id, key=key, value=value, updated_at=_now()))

def mem_get(db: Session, lead_id: int, key: str) -> Optional[str]:
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def require_admin(req: Request, token_from_form: str = "") -> bool:
    token = (token_from_form or req.headers.get("x-admin-token", "") or req.query_params.get("token", "") or "").strip()
    want = (os.getenv("ADMIN_TOKEN") or "").strip()
    if not want:
        return False
    return token == want


# =========================
# Timezone inference (simple + safe)
# =========================
def infer_timezone_from_phone(phone_e164: Optional[str]) -> Optional[str]:
    """
    Conservative:
    - If unknown, default to America/Denver (your timezone) instead of blocking outreach.
    - You can upgrade later with a real libphonenumber mapping.
    """
    if not phone_e164:
        return "America/Denver"
    # If it is +1 US/CA, assume your timezone for now.
    # Upgrade later: parse area code mapping.
    return (os.getenv("DEFAULT_TIMEZONE") or "America/Denver").strip()

def allowed_to_contact(lead: Lead) -> bool:
    tz_name = (lead.timezone or "").strip()
    if not tz_name:
        tz_name = (os.getenv("DEFAULT_TIMEZONE") or "America/Denver").strip()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/Denver")
    local_hour = datetime.now(tz).hour
    return 8 <= local_hour < 21


# =========================
# Owner escalation + forwarding rules
# =========================
def owner_mobile() -> str:
    return (os.getenv("OWNER_MOBILE") or os.getenv("TWILIO_OWNER_NUMBER") or os.getenv("TWILIO_TO_NUMBER") or "").strip()

def notify_owner(db: Session, lead: Optional[Lead], msg: str, tag: str = "OWNER_NOTIFY"):
    try:
        who = ""
        if lead:
            who = f"#{lead.id} {lead.full_name or 'Unknown'} {lead.phone or ''}".strip()
        payload = f"{tag}\n{who}\n\n{(msg or '').strip()}".strip()
        _log(db, lead.id if lead else None, None, tag, payload[:5000])
        db.commit()
    except Exception:
        pass

    try:
        if owner_mobile():
            send_alert_sms(payload)
    except Exception as e:
        try:
            _log(db, lead.id if lead else None, None, "OWNER_NOTIFY_FAILED", str(e)[:2000])
            db.commit()
        except Exception:
            pass

def cancel_pending_actions(db: Session, lead_id: int, reason: str):
    actions = db.query(Action).filter(Action.lead_id == lead_id, Action.status == "PENDING").all()
    for a in actions:
        a.status = "SKIPPED"
        a.error = f"Canceled: {reason}"
        a.finished_at = _now()
    _log(db, lead_id, None, "ACTIONS_CANCELED", reason)

def classify_inbound_text(body: str) -> str:
    t = (body or "").strip().lower()
    if any(x in t for x in ["stop", "unsubscribe", "do not contact", "dont contact", "dnc"]):
        return "STOP"
    if any(x in t for x in ["appointment", "appt", "schedule", "book", "tomorrow", "today", "am", "pm", "morning", "afternoon", "evening"]):
        return "APPT"
    if any(x in t for x in ["call me", "ready", "yes", "yep", "yeah", "now", "interested"]):
        return "HOT"
    if any(x in t for x in ["how much", "price", "cost", "quote", "coverage", "premium", "whole life", "term", "monthly"]):
        return "QUESTION"
    return "NEUTRAL"


# =========================
# PDF / OCR Extraction
# =========================
def extract_text_from_pdf_bytes(data: bytes) -> str:
    if not PDF_OK:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        chunks: List[str] = []
        for page in reader.pages:
            try:
                txt = page.extract_text() or ""
                if txt.strip():
                    chunks.append(txt)
            except Exception:
                continue
        return "\n".join(chunks)
    except Exception:
        return ""

def extract_text_from_image_bytes(data: bytes) -> str:
    if not OCR_OK:
        return ""
    try:
        img = Image.open(io.BytesIO(data))
        return pytesseract.image_to_string(img) or ""
    except Exception:
        return ""


# =========================
# Lead normalization (CSV + Text blocks)
# =========================
def normalize_to_leads(data: Any) -> List[Dict[str, Any]]:
    """
    Accepts:
      - list[dict] (CSV rows)
      - raw text string (PDF/image/doc extraction)
    Returns list of normalized lead dicts.
    """
    leads: List[Dict[str, Any]] = []

    # CSV rows
    if isinstance(data, list):
        for row in data:
            clean = {}
            for k, v in (row or {}).items():
                if not k:
                    continue
                kk = str(k).strip().lower()
                vv = v.strip() if isinstance(v, str) else v
                clean[kk] = vv

            first = clean.get("first name") or clean.get("firstname") or clean.get("first")
            last = clean.get("last name") or clean.get("lastname") or clean.get("last")
            full_name = clean.get("full name") or clean.get("name")
            if not full_name:
                full_name = f"{first or ''} {last or ''}".strip() or None

            phone = clean.get("phone") or clean.get("phone number") or clean.get("mobile") or clean.get("cell") or clean.get("cell phone")
            email = clean.get("email")
            st = clean.get("state") or clean.get("st")
            dob = clean.get("dob") or clean.get("date of birth") or clean.get("birthdate")
            cov = clean.get("coverage") or clean.get("coverage amount") or clean.get("desired coverage") or clean.get("desired coverage amount")
            cov_type = clean.get("coverage type") or clean.get("policy type") or clean.get("type")
            source = clean.get("source") or clean.get("lead source")
            ref = clean.get("lead id") or clean.get("reference") or clean.get("inquiry id") or clean.get("id")

            leads.append({
                "full_name": full_name,
                "phone": phone,
                "email": email,
                "state": st,
                "birthdate": dob,
                "coverage_requested": cov,
                "coverage_type": cov_type,
                "lead_source": source,
                "lead_reference": ref,
            })
        return leads

    # Text parse (PDF / OCR / Doc)
    raw = (data or "")
    if not isinstance(raw, str):
        raw = str(raw)

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    buf: Dict[str, Any] = {}

    phone_re = re.compile(r"(\+?1?\s*\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})")
    email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

    def flush():
        nonlocal buf
        if buf.get("phone") or buf.get("email"):
            leads.append(buf)
        buf = {}

    for line in lines:
        low = line.lower()

        # Split by common "Label: value" formats
        if ":" in line:
            left, right = line.split(":", 1)
            key = left.strip().lower()
            val = right.strip()

            if key in ["name", "full name"]:
                buf["full_name"] = val
            elif key in ["phone", "phone number", "mobile", "cell"]:
                buf["phone"] = val
            elif key in ["email"]:
                buf["email"] = val
            elif key in ["state", "st"]:
                buf["state"] = val
            elif key in ["dob", "date of birth", "birthdate"]:
                buf["birthdate"] = val
            elif "coverage" in key:
                buf["coverage_requested"] = val
            elif "type" in key:
                buf["coverage_type"] = val
            elif key in ["inquiry id", "lead id", "reference", "ref"]:
                buf["lead_reference"] = val
                flush()
            else:
                # ignore unknown keys
                pass
            continue

        # Otherwise pattern match
        m1 = phone_re.search(line)
        if m1:
            buf["phone"] = m1.group(1)

        m2 = email_re.search(line)
        if m2:
            buf["email"] = m2.group(0)

        # Detect likely name line (first non-empty, no digits)
        if "full_name" not in buf and len(line.split()) >= 2 and not any(c.isdigit() for c in line):
            # conservative: only if it's shortish
            if len(line) <= 40:
                buf["full_name"] = line

        # If we have enough to consider it a lead, and we hit a blank-ish separator next, flush
        if (buf.get("phone") or buf.get("email")) and ("---" in line or "====" in line):
            flush()

    flush()
    return leads


# =========================
# DB Insert helper (THIS is what your imports use)
# =========================
def _import_row(db: Session, row: Dict[str, Any]) -> bool:
    phone = normalize_phone(row.get("phone"))
    email = clean_text(row.get("email"))
    name = clean_text(row.get("full_name")) or "Unknown"

    if not phone:
        return False

    if dedupe_exists(db, phone, email):
        return False

    tz = infer_timezone_from_phone(phone)
    st = normalize_state(row.get("state"))
    birth = clean_text(row.get("birthdate"))
    cov = clean_text(row.get("coverage_requested"))
    cov_type = clean_text(row.get("coverage_type"))
    src = clean_text(row.get("lead_source"))
    ref = clean_text(row.get("lead_reference"))

    lead = Lead(
        full_name=name,
        phone=phone,
        email=email,
        state="NEW",                 # workflow state, not US state
        timezone=tz,
        us_state=st,                 # IMPORTANT: separate column (your models should have it; if not, we can switch to LeadMemory)
        birthdate=birth,
        coverage_requested=cov,
        coverage_type=cov_type,
        lead_source=src,
        lead_reference=ref,
        created_at=_now(),
        updated_at=_now(),
    )

    db.add(lead)
    return True
# =========================
# Health / Root / Service worker
# =========================
@app.get("/health")
def health():
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
    return {"ok": True}

@app.get("/")
def root():
    return RedirectResponse("/dashboard")

@app.get("/sw.js")
def sw():
    return Response(content="/* no-op service worker */", media_type="application/javascript")


# =========================
# AI chat (server-side) with offline fallback
# =========================
async def llm_reply(user_text: str, context: str = "") -> str:
    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return "AI is OFF (OPENAI_API_KEY missing). Use: counts | show pending | show newest | run planner | lead 123"

    model = (os.getenv("OPENAI_MODEL") or "gpt-5").strip()

    system = (
        "You are AgencyVault AI Employee for a life insurance agency.\n"
        "Be direct and practical.\n"
        "Never invent database values.\n"
        "If lead replied with a question, recommend that Nick calls personally.\n"
    )
    if context:
        system += "\nLive context:\n" + context

    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ],
    }

    async with httpx.AsyncClient(timeout=35) as client:
        r = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            return f"AI error {r.status_code}: {(r.text or '')[:400]}"

        data = r.json()

    try:
        out = data.get("output", [])
        chunks = []
        for item in out:
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    chunks.append(c.get("text", ""))
        text_out = "\n".join([x for x in chunks if x]).strip()
        return text_out or "AI returned no text."
    except Exception:
        return "AI response parsing failed."

def offline_assistant(db: Session, msg: str) -> str:
    low = (msg or "").strip().lower()
    if not low:
        return "Type: counts | show pending | show newest | run planner | lead 123"

    if "counts" in low:
        total = db.query(Lead).count()
        new = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        contacted = db.query(Lead).filter(Lead.state == "CONTACTED").count()
        dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()
        pend = db.query(Action).filter(Action.status == "PENDING").count()
        return (
            "Counts:\n"
            f"- total={total}\n"
            f"- NEW={new}\n"
            f"- WORKING={working}\n"
            f"- CONTACTED={contacted}\n"
            f"- DNC={dnc}\n"
            f"- pending_actions={pend}"
        )

    if "show" in low and "pending" in low:
        actions = (
            db.query(Action)
            .filter(Action.status == "PENDING")
            .order_by(Action.created_at.asc())
            .limit(25)
            .all()
        )
        if not actions:
            return "No pending actions."
        return "Pending actions:\n" + "\n".join([f"- #{a.id} {a.type} lead={a.lead_id} created={a.created_at}" for a in actions])

    if "show" in low and ("newest" in low or "recent" in low):
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(15).all()
        if not leads:
            return "No leads found."
        return "Newest leads:\n" + "\n".join([f"- #{l.id} {l.full_name} {l.phone} [{l.state}]" for l in leads])

    if low.startswith("lead "):
        nums = re.findall(r"\d+", low)
        if not nums:
            return "Usage: lead 123"
        lead_id = int(nums[0])
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return f"No lead with id {lead_id}"
        return (
            f"Lead #{lead.id}\n"
            f"Name: {lead.full_name}\n"
            f"Phone: {lead.phone}\n"
            f"Email: {lead.email or '-'}\n"
            f"Workflow: {lead.state}\n"
            f"US State: {getattr(lead, 'us_state', None) or '-'}\n"
            f"Timezone: {lead.timezone or '-'}\n"
            f"Coverage: {getattr(lead, 'coverage_requested', None) or '-'}\n"
        )

    if "run" in low and "planner" in low:
        out = plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
        return json.dumps(out, indent=2)

    return "Try: counts | show pending | show newest | run planner | lead 123"


# =========================
# Planner (creates PENDING actions)
# IMPORTANT: payload_json uses keys your worker can execute:
#   TEXT -> {"to": "...", "message": "..."}
#   CALL -> {"to": "...", "lead_id": 123}
# =========================
def plan_actions(db: Session, batch_size: int = 25) -> Dict[str, Any]:
    paused = (mem_get(db, 0, "GLOBAL_PAUSE") or "0") == "1"
    if paused:
        return {"ok": False, "paused": True, "message": "AI work is PAUSED by operator."}

    run = AgentRun(mode="planning", status="STARTED", batch_size=batch_size, notes="Planner run")
    db.add(run)
    db.flush()

    planned = 0
    considered = 0
    nowv = _now()

    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW", Lead.phone.isnot(None))
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        considered += 1

        # Ensure timezone exists
        if not (lead.timezone or "").strip():
            lead.timezone = infer_timezone_from_phone(lead.phone)

        first = safe_first_name(lead.full_name)

        msg1 = (
            f"Hi{(' ' + first) if first else ''}, this is Nick's office. "
            "You requested life insurance information. "
            "Would you like a quick quote today?"
        )

        db.add(Action(
            lead_id=lead.id,
            type="TEXT",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({
                "to": lead.phone,
                "message": msg1
            }),
            created_at=nowv,
        ))
        planned += 1

        # Optional call follow-up (15 min later)
        call_due = (nowv + timedelta(minutes=15)).isoformat()
        db.add(Action(
            lead_id=lead.id,
            type="CALL",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({
                "to": lead.phone,
                "lead_id": lead.id,
                "due_at": call_due
            }),
            created_at=nowv,
        ))
        planned += 1

        lead.state = "WORKING"
        lead.updated_at = nowv

    run.status = "SUCCEEDED"
    run.finished_at = _now()
    db.commit()

    _log(db, None, run.id, "AI_PLANNED", f"planned={planned} considered={considered}")
    db.commit()

    return {"ok": True, "run_id": run.id, "planned_actions": planned, "considered": considered}


@app.get("/ai/plan")
def ai_plan():
    db = SessionLocal()
    try:
        return plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
    finally:
        db.close()


# =========================
# Assistant API
# =========================
@app.post("/api/assistant")
async def assistant_api(payload: dict):
    msg = (payload.get("message") or "").strip()
    db = SessionLocal()
    try:
        _log(db, None, None, "ASSISTANT_COMMAND", msg)
        db.commit()

        if not msg:
            return {"reply": "Try: counts | show pending | show newest | run planner | lead 123"}

        low = msg.lower()
        if "run" in low and "planner" in low:
            out = plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
            _log(db, None, out.get("run_id"), "ASSISTANT_RESULT", f"Planner ran: {out}")
            db.commit()
            return {"reply": json.dumps(out, indent=2)}

        api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            reply = offline_assistant(db, msg)
            _log(db, None, None, "ASSISTANT_OFFLINE_REPLY", reply[:2000])
            db.commit()
            return {"reply": reply}

        total = db.query(Lead).count()
        new = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()
        pending = db.query(Action).filter(Action.status == "PENDING").count()
        paused = (mem_get(db, 0, "GLOBAL_PAUSE") or "0") == "1"

        failed = (
            db.query(Action)
            .filter(Action.status == "FAILED")
            .order_by(Action.finished_at.desc().nullslast(), Action.id.desc())
            .limit(5)
            .all()
        )
        failed_lines = [f"Action#{a.id} {a.type} lead={a.lead_id} err={(a.error or '')[:160]}" for a in failed]

        context = (
            f"Counts: total={total} NEW={new} WORKING={working} DNC={dnc} pending_actions={pending} paused={paused}\n"
            "Recent failures:\n" + ("\n".join(failed_lines) if failed_lines else "None.")
        )

        reply = await llm_reply(msg, context=context)
        _log(db, None, None, "ASSISTANT_REPLY", reply[:2000])
        db.commit()
        return {"reply": reply}
    finally:
        db.close()


# =========================
# Dashboard helpers
# =========================
def _kpi_card(label: str, value: Any, sub: str = "") -> str:
    return f"""
    <div class="kpi">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value">{value}</div>
      <div class="kpi-sub">{sub}</div>
    </div>
    """

def _svg_donut(pct: float) -> str:
    pct = max(0.0, min(100.0, pct))
    r = 16
    c = 2 * 3.14159 * r
    dash = (pct / 100.0) * c
    gap = c - dash
    return f"""
    <svg width="44" height="44" viewBox="0 0 44 44">
      <circle cx="22" cy="22" r="{r}" fill="none" stroke="rgba(138,180,248,.15)" stroke-width="6"></circle>
      <circle cx="22" cy="22" r="{r}" fill="none" stroke="rgba(138,180,248,.95)" stroke-width="6"
              stroke-dasharray="{dash:.2f} {gap:.2f}" transform="rotate(-90 22 22)"></circle>
      <text x="22" y="25" text-anchor="middle" font-size="10" fill="rgba(230,237,243,.85)">{pct:.0f}%</text>
    </svg>
    """


# =========================
# Dashboard
# =========================
@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    db = SessionLocal()
    try:
        total = db.query(Lead).count()
        new = db.query(Lead).filter(Lead.state == "NEW").count()
        working = db.query(Lead).filter(Lead.state == "WORKING").count()
        contacted = db.query(Lead).filter(Lead.state == "CONTACTED").count()
        dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()
        pending = db.query(Action).filter(Action.status == "PENDING").count()
        paused = (mem_get(db, 0, "GLOBAL_PAUSE") or "0") == "1"

        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(18).all()
        feed = ""
        for l in logs:
            feed += f"""
            <div class="feed-item">
              <div class="feed-top">
                <div class="feed-title">{l.event}</div>
                <div class="feed-time">{l.created_at}</div>
              </div>
              <div class="feed-meta">lead={l.lead_id} run={l.run_id}</div>
              <div class="feed-body">{(l.detail or "")[:280]}</div>
            </div>
            """

        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(10).all()
        leads_html = ""
        for l in leads:
            us_state = getattr(l, "us_state", None) or "-"
            cov = getattr(l, "coverage_requested", None) or "-"
            leads_html += f"""
            <div class="lead-row">
              <div>
                <div class="lead-name"><a href="/leads/{l.id}">#{l.id} {l.full_name or "Unknown"}</a></div>
                <div class="lead-meta">{l.phone or "-"} | {l.email or "-"}</div>
                <div class="lead-meta">US State: {us_state} | Coverage: {cov}</div>
              </div>
              <div class="lead-badges">
                <span class="pill">{l.state}</span>
                <form method="post" action="/leads/delete/{l.id}" style="margin:0"
                      onsubmit="return confirm('Delete lead #{l.id}?');">
                  <button class="mini danger" type="submit">Delete</button>
                </form>
              </div>
            </div>
            """

        denom = max(total, 1)
        pct_new = (new / denom) * 100.0
        pct_working = (working / denom) * 100.0
        pct_contacted = (contacted / denom) * 100.0
        pct_dnc = (dnc / denom) * 100.0

        pause_label = "Resume Work" if paused else "Pause Work"
        pause_sub = "Paused" if paused else "Running"

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AgencyVault - Command Center</title>
<style>
  :root {{
    --bg:#0b0f17;
    --panel:#0f1624;
    --panel2:#0b1220;
    --border:rgba(50,74,110,.25);
    --text:#e6edf3;
    --muted:rgba(230,237,243,.72);
    --link:#8ab4f8;
  }}
  body {{ margin:0; background:var(--bg); color:var(--text); font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial; }}
  a {{ color:var(--link); text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .wrap {{ display:grid; grid-template-columns: 260px 1fr 360px; min-height:100vh; }}
  .sidebar {{ border-right:1px solid var(--border); padding:16px 14px; position:sticky; top:0; height:100vh; overflow:auto;
             background:linear-gradient(180deg, rgba(17,24,39,.55), rgba(11,15,23,.55)); }}
  .brand {{ font-weight:900; letter-spacing:.2px; font-size:20px; margin-bottom:10px; }}
  .nav {{ display:flex; flex-direction:column; gap:8px; margin-top:12px; }}
  .nav a {{ display:flex; align-items:center; gap:10px; padding:10px 12px; border-radius:12px; border:1px solid rgba(50,74,110,.15); background:rgba(15,22,36,.55); }}
  .nav a:hover {{ border-color:rgba(138,180,248,.55); }}
  .nav small {{ color:var(--muted); display:block; margin-left:28px; margin-top:-6px; }}

  .main {{ padding:18px 18px 30px 18px; }}
  .topbar {{ display:flex; justify-content:space-between; align-items:flex-end; gap:14px; flex-wrap:wrap; margin-bottom:14px; }}
  .title {{ font-size:28px; font-weight:900; }}
  .subtitle {{ color:var(--muted); font-size:13px; }}
  .kpis {{ display:grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap:10px; margin-top:12px; }}
  .kpi {{ background:var(--panel); border:1px solid var(--border); border-radius:16px; padding:12px; }}
  .kpi-label {{ color:var(--muted); font-size:12px; }}
  .kpi-value {{ font-size:22px; font-weight:900; margin-top:2px; }}
  .kpi-sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}

  .grid {{ display:grid; grid-template-columns: 1fr 1fr; gap:12px; margin-top:12px; }}
  .panel {{ background:var(--panel); border:1px solid var(--border); border-radius:18px; padding:14px; }}
  .panel h2 {{ margin:0 0 10px 0; font-size:16px; font-weight:900; letter-spacing:.2px; }}

  .feed-item {{ padding:10px 0; border-bottom:1px solid var(--border); }}
  .feed-top {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
  .feed-title {{ font-weight:800; }}
  .feed-time {{ color:var(--muted); font-size:12px; }}
  .feed-meta {{ color:var(--muted); font-size:12px; margin-top:2px; }}
  .feed-body {{ margin-top:6px; color:rgba(230,237,243,.9); }}

  .lead-row {{ display:flex; justify-content:space-between; align-items:center; gap:10px; padding:10px 0; border-bottom:1px solid var(--border); }}
  .lead-name {{ font-weight:800; }}
  .lead-meta {{ color:var(--muted); font-size:12px; margin-top:2px; }}
  .lead-badges {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; justify-content:flex-end; }}
  .pill {{ padding:3px 9px; border-radius:999px; border:1px solid rgba(138,180,248,.25); background:rgba(17,24,39,.6); font-size:12px; color:rgba(230,237,243,.9); }}

  .right {{ border-left:1px solid var(--border); padding:16px 14px; position:sticky; top:0; height:100vh; overflow:auto;
            background:linear-gradient(180deg, rgba(15,22,36,.55), rgba(11,15,23,.55)); }}

  .btn {{ display:inline-flex; align-items:center; justify-content:center; gap:8px; padding:10px 12px; border-radius:12px;
         border:1px solid rgba(50,74,110,.35); background:rgba(17,24,39,.75); color:var(--text); cursor:pointer; text-decoration:none; font-weight:700; }}
  .btn:hover {{ border-color:rgba(138,180,248,.6); }}
  .mini {{ padding:7px 10px; border-radius:10px; border:1px solid rgba(50,74,110,.35); background:rgba(17,24,39,.75); color:var(--text);
          cursor:pointer; font-weight:800; font-size:12px; }}
  .danger {{ background:rgba(192,58,58,.18); border-color:rgba(192,58,58,.35); }}
  .danger:hover {{ border-color:rgba(192,58,58,.65); }}

  textarea {{ width:100%; background:rgba(11,15,23,.75); color:var(--text); border:1px solid rgba(50,74,110,.35); border-radius:14px;
            padding:12px; min-height:110px; font-size:14px; outline:none; }}
  pre {{ white-space:pre-wrap; margin:10px 0 0 0; color:rgba(230,237,243,.9); font-size:13px; }}
  input, select {{ width:100%; background:rgba(11,15,23,.75); color:var(--text); border:1px solid rgba(50,74,110,.35); border-radius:12px; padding:10px; outline:none; }}
  .muted {{ color:var(--muted); font-size:12px; }}

  .donuts {{ display:grid; grid-template-columns: repeat(2, 1fr); gap:10px; }}
  .donut-card {{ background:var(--panel2); border:1px solid var(--border); border-radius:16px; padding:12px; display:flex; align-items:center; justify-content:space-between; gap:10px; }}
  .donut-label {{ font-weight:900; }}
  .donut-sub {{ color:var(--muted); font-size:12px; margin-top:2px; }}

  @media (max-width: 1100px) {{
    .wrap {{ grid-template-columns: 1fr; }}
    .sidebar, .right {{ position:relative; height:auto; border:none; }}
    .kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>

<div class="wrap">

  <aside class="sidebar">
    <div class="brand">AgencyVault</div>
    <div class="subtitle">Life Insurance Command Center</div>

    <div class="nav">
      <a href="/dashboard">Dashboard<small>Command Center</small></a>
      <a href="/leads">All Leads<small>Search and filter</small></a>
      <a href="/leads/new">Add Lead<small>Manual entry</small></a>
      <a href="/imports">Imports<small>CSV, Google, Images, PDF</small></a>
      <a href="/actions">Action Queue<small>Calls/texts planned</small></a>
      <a href="/activity">Activity Log<small>What happened</small></a>
      <a href="/admin">Admin<small>Mass delete and pause</small></a>
      <a href="/ai/plan">Run Planner<small>Create PENDING actions</small></a>
    </div>

    <div style="margin-top:14px" class="muted">
      Tip: AI box accepts "counts", "show pending", "show newest", "run planner".
    </div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div>
        <div class="title">Command Center</div>
        <div class="subtitle">Everything important is visible right here.</div>
      </div>
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <a class="btn" href="/leads/new">Add Lead</a>
        <a class="btn" href="/leads">Browse Leads</a>
        <a class="btn" href="/imports">Import</a>
      </div>
    </div>

    <div class="kpis">
      {_kpi_card("Total Leads", total, "All time")}
      {_kpi_card("NEW", new, "Not touched yet")}
      {_kpi_card("WORKING", working, "In outreach")}
      {_kpi_card("CONTACTED", contacted, "Hot replies / progressed")}
      {_kpi_card("DNC", dnc, "Compliance")}
      {_kpi_card("Pending Actions", pending, f"Planner {pause_sub}")}
    </div>

    <div class="grid">
      <div class="panel">
        <h2>Distribution</h2>
        <div class="donuts">
          <div class="donut-card"><div><div class="donut-label">NEW</div><div class="donut-sub">{new} of {total}</div></div>{_svg_donut(pct_new)}</div>
          <div class="donut-card"><div><div class="donut-label">WORKING</div><div class="donut-sub">{working} of {total}</div></div>{_svg_donut(pct_working)}</div>
          <div class="donut-card"><div><div class="donut-label">CONTACTED</div><div class="donut-sub">{contacted} of {total}</div></div>{_svg_donut(pct_contacted)}</div>
          <div class="donut-card"><div><div class="donut-label">DNC</div><div class="donut-sub">{dnc} of {total}</div></div>{_svg_donut(pct_dnc)}</div>
        </div>
      </div>

      <div class="panel">
        <h2>Newest Leads</h2>
        <div>{leads_html or '<div class="muted">No leads yet.</div>'}</div>
      </div>

      <div class="panel">
        <h2>Live Activity Feed</h2>
        <div>{feed or '<div class="muted">No activity yet.</div>'}</div>
      </div>

      <div class="panel">
        <h2>Schedule</h2>
        <div class="muted" style="margin-bottom:10px;">
          Calendar sync will be added after imports + texting is confirmed stable.
        </div>
      </div>
    </div>
  </main>

  <aside class="right">
    <div class="panel" style="padding:14px;">
      <h2>AI Employee</h2>
      <div class="muted">If OpenAI is off, it uses Offline Assistant.</div>
      <textarea id="cmd" placeholder="Try: counts | show pending | show newest | lead 12 | run planner"></textarea>
      <div style="display:flex; gap:10px; margin-top:10px;">
        <button class="btn" onclick="send()">Send</button>
        <button class="btn" onclick="document.getElementById('cmd').value='counts'; send();">Counts</button>
        <button class="btn" onclick="document.getElementById('cmd').value='run planner'; send();">Run Planner</button>
      </div>
      <pre id="out" class="muted"></pre>
    </div>

    <div class="panel" style="margin-top:12px;">
      <h2 id="imports">Imports (Right Here)</h2>

      <div class="muted" style="margin-bottom:8px;">Upload CSV</div>
      <form action="/import/csv" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".csv" />
        <div style="margin-top:8px;"><button class="btn" type="submit">Upload CSV</button></div>
      </form>

      <div class="muted" style="margin:14px 0 8px;">Upload Image (JPG/PNG)</div>
      <form action="/import/image" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept="image/*" />
        <div style="margin-top:8px;"><button class="btn" type="submit">Upload Image</button></div>
      </form>

      <div class="muted" style="margin:14px 0 8px;">Upload PDF (typed PDFs)</div>
      <form action="/import/pdf" method="post" enctype="multipart/form-data">
        <input type="file" name="file" accept=".pdf,application/pdf" />
        <div style="margin-top:8px;"><button class="btn" type="submit">Upload PDF</button></div>
      </form>
    </div>

  </aside>

</div>

<script>
async function send() {{
  const msg = document.getElementById("cmd").value;
  const out = document.getElementById("out");
  out.textContent = "Thinking...";
  try {{
    const r = await fetch("/api/assistant", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ message: msg }})
    }});
    const d = await r.json();
    out.textContent = d.reply || "OK";
  }} catch (e) {{
    out.textContent = "Error: " + e;
  }}
}}
</script>
</body>
</html>
        """)
    finally:
        db.close()


@app.get("/imports", response_class=HTMLResponse)
def imports_page():
    return RedirectResponse("/dashboard#imports", status_code=303)


# =========================
# Imports (ONE route each - no duplicates)
# =========================
@app.post("/import/csv")
async def import_csv(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        raw_bytes = await file.read()
        if not raw_bytes:
            return HTMLResponse("<div style='font-family:system-ui;padding:20px;color:#ffb4b4'>Empty upload</div>", status_code=400)

        raw = raw_bytes.decode("utf-8", errors="ignore")
        reader = csv.DictReader(io.StringIO(raw))
        rows = list(reader)

        leads = normalize_to_leads(rows)

        added = 0
        for lead in leads:
            if _import_row(db, lead):
                added += 1

        _log(db, None, None, "IMPORT_CSV", f"rows={len(rows)} leads={len(leads)} imported={added}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    except Exception as e:
        _log(db, None, None, "IMPORT_CSV_ERROR", str(e)[:2000])
        db.commit()
        return HTMLResponse(f"<div style='font-family:system-ui;padding:20px;color:#ffb4b4'>CSV Import Error: {str(e)[:500]}</div>", status_code=400)
    finally:
        db.close()


@app.post("/import/pdf")
async def import_pdf(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        data = await file.read()
        if not data:
            return HTMLResponse("<div style='font-family:system-ui;padding:20px;color:#ffb4b4'>Empty upload</div>", status_code=400)

        text_data = extract_text_from_pdf_bytes(data)
        if not (text_data or "").strip():
            _log(db, None, None, "IMPORT_PDF_EMPTY", "No readable PDF text")
            db.commit()
            return RedirectResponse("/dashboard", status_code=303)

        leads = normalize_to_leads(text_data)
        added = 0
        for lead in leads:
            if _import_row(db, lead):
                added += 1

        _log(db, None, None, "IMPORT_PDF", f"imported={added}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    except Exception as e:
        _log(db, None, None, "IMPORT_PDF_ERROR", str(e)[:2000])
        db.commit()
        return HTMLResponse(f"<div style='font-family:system-ui;padding:20px;color:#ffb4b4'>PDF Import Error: {str(e)[:500]}</div>", status_code=400)
    finally:
        db.close()


@app.post("/import/image")
async def import_image(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        data = await file.read()
        if not data:
            return HTMLResponse("<div style='font-family:system-ui;padding:20px;color:#ffb4b4'>Empty upload</div>", status_code=400)

        text_data = extract_text_from_image_bytes(data)
        if not (text_data or "").strip():
            _log(db, None, None, "IMPORT_IMAGE_EMPTY", "OCR not available or no text extracted")
            db.commit()
            return RedirectResponse("/dashboard", status_code=303)

        leads = normalize_to_leads(text_data)
        added = 0
        for lead in leads:
            if _import_row(db, lead):
                added += 1

        _log(db, None, None, "IMPORT_IMAGE", f"imported={added}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    except Exception as e:
        _log(db, None, None, "IMPORT_IMAGE_ERROR", str(e)[:2000])
        db.commit()
        return HTMLResponse(f"<div style='font-family:system-ui;padding:20px;color:#ffb4b4'>Image Import Error: {str(e)[:500]}</div>", status_code=400)
    finally:
        db.close()
# =========================
# Leads: Add + List + Detail + Delete
# =========================
@app.get("/leads/new", response_class=HTMLResponse)
def leads_new_form():
    return HTMLResponse("""
    <html><body style="font-family:system-ui;padding:24px;background:#0b0f17;color:#e6edf3;max-width:900px;margin:0 auto;">
      <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;"><- Back</a>
      <h2 style="margin-top:12px;">Add Lead</h2>
      <form method="post" action="/leads/new" style="margin-top:14px;">
        <div style="opacity:.8">Full Name</div>
        <input name="full_name" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
        <br><br>
        <div style="opacity:.8">Phone</div>
        <input name="phone" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
        <br><br>
        <div style="opacity:.8">Email</div>
        <input name="email" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
        <br><br>
        <button type="submit" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:800;">
          Create Lead
        </button>
      </form>
    </body></html>
    """)

@app.post("/leads/new")
def leads_new(full_name: str = Form(""), phone: str = Form(""), email: str = Form("")):
    db = SessionLocal()
    try:
        p = normalize_phone(phone)
        e = clean_text(email)
        n = clean_text(full_name) or "Unknown"

        if not p:
            return HTMLResponse("<div style='color:#ffb4b4;font-family:system-ui;padding:20px'>Invalid phone.</div>", status_code=400)

        if dedupe_exists(db, p, e):
            return HTMLResponse("<div style='color:#ffb4b4;font-family:system-ui;padding:20px'>Duplicate lead.</div>", status_code=409)

        tz = infer_timezone_from_phone(p)

        db.add(Lead(
            full_name=n,
            phone=p,
            email=e,
            state="NEW",
            timezone=tz,
            created_at=_now(),
            updated_at=_now(),
        ))

        _log(db, None, None, "LEAD_CREATED", f"{n} {p}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.get("/leads", response_class=HTMLResponse)
def leads_list(search: str = "", state: str = ""):
    db = SessionLocal()
    try:
        q = db.query(Lead)
        if state:
            q = q.filter(Lead.state == state)
        if search:
            s = f"%{search.strip()}%"
            q = q.filter((Lead.full_name.ilike(s)) | (Lead.phone.ilike(s)) | (Lead.email.ilike(s)))
        leads = q.order_by(Lead.created_at.desc()).limit(250).all()

        rows = ""
        for l in leads:
            us_state = getattr(l, "us_state", None) or "-"
            cov = getattr(l, "coverage_requested", None) or "-"
            rows += f"""
            <div style="padding:12px 0;border-bottom:1px solid rgba(50,74,110,.25);display:flex;justify-content:space-between;gap:12px;align-items:center;">
              <div>
                <div style="font-weight:900;"><a href="/leads/{l.id}">#{l.id} {l.full_name or "Unknown"}</a> <span style="opacity:.75;font-weight:700;">[{l.state}]</span></div>
                <div style="opacity:.75;font-size:13px;margin-top:2px;">Phone: {l.phone or "-"} | Email: {l.email or "-"}</div>
                <div style="opacity:.75;font-size:13px;margin-top:2px;">US State: {us_state} | Coverage: {cov}</div>
              </div>
              <div style="display:flex;gap:10px;align-items:center;">
                <a href="/leads/{l.id}" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:8px 12px;border-radius:12px;text-decoration:none;font-weight:800;">Open</a>
                <form method="post" action="/leads/delete/{l.id}" style="margin:0" onsubmit="return confirm('Delete lead #{l.id}?');">
                  <button type="submit" style="background:rgba(192,58,58,.18);border:1px solid rgba(192,58,58,.35);color:#e6edf3;padding:8px 12px;border-radius:12px;cursor:pointer;font-weight:900;">
                    Delete
                  </button>
                </form>
              </div>
            </div>
            """

        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;">
          <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;"><- Back</a>
          <h2 style="margin-top:12px;">All Leads</h2>

          <form method="get" action="/leads" style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;">
            <input name="search" value="{(search or '').replace('"','&quot;')}" placeholder="Search name/phone/email"
                   style="flex:1;min-width:240px;padding:10px;border-radius:12px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
            <select name="state" style="padding:10px;border-radius:12px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3">
              <option value="">All workflow states</option>
              <option value="NEW" {"selected" if state=="NEW" else ""}>NEW</option>
              <option value="WORKING" {"selected" if state=="WORKING" else ""}>WORKING</option>
              <option value="CONTACTED" {"selected" if state=="CONTACTED" else ""}>CONTACTED</option>
              <option value="APPT_PENDING" {"selected" if state=="APPT_PENDING" else ""}>APPT_PENDING</option>
              <option value="DO_NOT_CONTACT" {"selected" if state=="DO_NOT_CONTACT" else ""}>DO_NOT_CONTACT</option>
            </select>
            <button type="submit" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
              Filter
            </button>
            <a href="/leads/new" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;text-decoration:none;font-weight:900;">
              Add Lead
            </a>
          </form>

          <div style="background:#0f1624;border:1px solid rgba(50,74,110,.25);border-radius:16px;padding:14px;">
            {rows or "<div style='opacity:.75'>No leads found.</div>"}
          </div>
        </body></html>
        """)
    finally:
        db.close()

@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Not found", status_code=404)

        actions = db.query(Action).filter(Action.lead_id == lead_id).order_by(Action.id.desc()).limit(120).all()
        logs = db.query(AuditLog).filter(AuditLog.lead_id == lead_id).order_by(AuditLog.created_at.desc()).limit(250).all()
        msgs = db.query(Message).filter(Message.lead_id == lead_id).order_by(Message.created_at.desc()).limit(80).all()

        def block(title, content):
            return f"""
            <div style="margin-top:22px">
              <h3 style="margin:0 0 10px 0">{title}</h3>
              <div style="background:#0f1624;border:1px solid rgba(50,74,110,.25);border-radius:16px;padding:14px;">
                {content}
              </div>
            </div>
            """

        msg_html = "".join(
            f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,.2)">
              <div style="font-weight:900">{m.direction} - {m.channel}</div>
              <div style="opacity:.7;font-size:12px">{m.created_at}</div>
              <div style="margin-top:6px;white-space:pre-wrap">{m.body}</div>
            </div>
            """ for m in msgs
        ) or "<div style='opacity:.65'>No messages</div>"

        action_html = "".join(
            f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,.2)">
              <div style="font-weight:900">#{a.id} - {a.type} <span style="opacity:.75">[{a.status}]</span></div>
              <div style="opacity:.7;font-size:12px">Tool: {a.tool}</div>
              <pre style="margin-top:6px;white-space:pre-wrap">{a.payload_json}</pre>
              {f"<div style='color:#ffb4b4;font-weight:800'>Error: {a.error}</div>" if a.error else ""}
            </div>
            """ for a in actions
        ) or "<div style='opacity:.65'>No actions</div>"

        log_html = "".join(
            f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,.2)">
              <div style="font-weight:900">{l.event}</div>
              <div style="opacity:.7;font-size:12px">{l.created_at}</div>
              <div style="margin-top:6px;white-space:pre-wrap">{l.detail}</div>
            </div>
            """ for l in logs
        ) or "<div style='opacity:.65'>No activity</div>"

        us_state = getattr(lead, "us_state", None) or "-"
        cov = getattr(lead, "coverage_requested", None) or "-"
        cov_type = getattr(lead, "coverage_type", None) or "-"
        tz = lead.timezone or "-"

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Lead #{lead.id}</title>
</head>
<body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;">
<a href="/dashboard" style="color:#8ab4f8;text-decoration:none;"><- Back</a>

<h1 style="margin:14px 0 6px 0">#{lead.id} - {lead.full_name or "Unknown"}</h1>
<div style="display:flex;gap:18px;flex-wrap:wrap;opacity:.92">
  <div>Phone: <b>{lead.phone or "-"}</b></div>
  <div>Email: <b>{lead.email or "-"}</b></div>
  <div>Workflow: <b>{lead.state}</b></div>
  <div>US State: <b>{us_state}</b></div>
  <div>Timezone: <b>{tz}</b></div>
  <div>Coverage: <b>{cov}</b></div>
  <div>Type: <b>{cov_type}</b></div>
</div>

<div style="margin-top:14px; display:flex; gap:10px; flex-wrap:wrap;">
  <form method="post" action="/leads/delete/{lead.id}" onsubmit="return confirm('Delete this lead permanently?');" style="margin:0;">
    <button style="background:rgba(192,58,58,.18);border:1px solid rgba(192,58,58,.35);color:#e6edf3;padding:10px 14px;border-radius:14px;cursor:pointer;font-weight:900;">
      Delete Lead
    </button>
  </form>
  <a href="/leads" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:14px;text-decoration:none;font-weight:900;">
    Back to Leads
  </a>
</div>

{block("Messages", msg_html)}
{block("Actions", action_html)}
{block("Activity Log", log_html)}
</body>
</html>
        """)
    finally:
        db.close()

@app.post("/leads/delete/{lead_id}")
def delete_lead(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if lead:
            _log(db, lead.id, None, "LEAD_DELETED", f"{lead.full_name} {lead.phone}")
            db.delete(lead)
            db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()


# =========================
# Actions / Activity
# =========================
@app.get("/actions", response_class=HTMLResponse)
def actions_page():
    db = SessionLocal()
    try:
        actions = db.query(Action).order_by(Action.id.desc()).limit(600).all()
        rows = ""
        for a in actions:
            rows += f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,.25)">
              <b>#{a.id} {a.type}</b> lead={a.lead_id} <span style="opacity:.75">[{a.status}]</span>
              <div style="opacity:.75;font-size:12px">{a.created_at} - tool={a.tool}</div>
              <div style="opacity:.9;white-space:pre-wrap">{(a.payload_json or "")[:260]}</div>
              <div style="color:#ffb4b4;opacity:.95">{(a.error or "")[:260]}</div>
            </div>
            """
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;">
        <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;"><- Back</a>
        <h2>Action Queue</h2>
        <div style="background:#0f1624;padding:14px;border-radius:16px;border:1px solid rgba(50,74,110,.25)">
          {rows or "No actions"}
        </div>
        </body></html>
        """)
    finally:
        db.close()

@app.get("/activity", response_class=HTMLResponse)
def activity():
    db = SessionLocal()
    try:
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(700).all()
        rows = ""
        for l in logs:
            rows += f"""
            <div style="padding:10px 0;border-bottom:1px solid rgba(50,74,110,.25)">
              <b>{l.event}</b>
              <span style="opacity:.75">lead={l.lead_id} run={l.run_id} - {l.created_at}</span>
              <div style="white-space:pre-wrap;opacity:.95">{l.detail}</div>
            </div>
            """
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;">
        <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;"><- Back</a>
        <h2>Activity</h2>
        <div style="background:#0f1624;padding:14px;border-radius:16px;border:1px solid rgba(50,74,110,.25)">
          {rows or "No activity"}
        </div>
        </body></html>
        """)
    finally:
        db.close()


# =========================
# Admin
# =========================
@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return HTMLResponse("""
    <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:900px;margin:0 auto;">
      <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;"><- Back</a>
      <h2>Admin</h2>
      <div style="opacity:.8;margin-bottom:14px">
        Dangerous actions require ADMIN_TOKEN.
      </div>

      <div style="background:#0f1624;border:1px solid rgba(50,74,110,.25);border-radius:16px;padding:14px;">
        <h3 style="margin:0 0 10px 0;color:#ffb4b4">Delete ALL Leads</h3>
        <form method="post" action="/admin/wipe" onsubmit="return confirm('This permanently deletes everything. Are you sure?');">
          <div style="opacity:.8">Type DELETE ALL LEADS:</div>
          <input name="confirm" style="margin-top:8px;padding:10px;border-radius:12px;border:1px solid rgba(50,74,110,.35);background:#0b0f17;color:#e6edf3;width:320px" />
          <div style="margin-top:10px;">
            <input name="token" placeholder="ADMIN_TOKEN" style="padding:10px;border-radius:12px;border:1px solid rgba(50,74,110,.35);background:#0b0f17;color:#e6edf3;width:320px" />
          </div>
          <button style="margin-top:10px;background:rgba(192,58,58,.18);border:1px solid rgba(192,58,58,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
            Permanently Delete
          </button>
        </form>

        <h3 style="margin:18px 0 10px 0;">Pause or Resume Work</h3>
        <form method="post" action="/admin/pause-toggle">
          <input name="token" placeholder="ADMIN_TOKEN" style="padding:10px;border-radius:12px;border:1px solid rgba(50,74,110,.35);background:#0b0f17;color:#e6edf3;width:320px" />
          <button style="margin-left:10px;background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
            Toggle Pause
          </button>
        </form>
      </div>
    </body></html>
    """)

@app.post("/admin/pause-toggle")
def admin_pause_toggle(request: Request, token: str = Form("")):
    if not require_admin(request, token_from_form=token):
        return HTMLResponse("<div style='font-family:system-ui;padding:20px;color:#ffb4b4'>Missing or invalid ADMIN_TOKEN.</div>", status_code=401)

    db = SessionLocal()
    try:
        paused = (mem_get(db, 0, "GLOBAL_PAUSE") or "0") == "1"
        mem_set(db, 0, "GLOBAL_PAUSE", "0" if paused else "1")
        _log(db, None, None, "GLOBAL_PAUSE_TOGGLED", f"paused={'0' if paused else '1'}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.post("/admin/wipe")
async def admin_wipe(request: Request, confirm: str = Form(...), token: str = Form("")):
    if confirm.strip() != "DELETE ALL LEADS":
        return JSONResponse({"error": "Confirmation text does not match"}, status_code=400)
    if not require_admin(request, token_from_form=token):
        return JSONResponse({"error": "Missing or invalid ADMIN_TOKEN"}, status_code=401)

    db = SessionLocal()
    try:
        db.execute(text("DELETE FROM messages"))
        db.execute(text("DELETE FROM audit_log"))
        db.execute(text("DELETE FROM lead_memory"))
        db.execute(text("DELETE FROM actions"))
        db.execute(text("DELETE FROM agent_runs"))
        db.execute(text("DELETE FROM leads"))
        db.commit()
        return JSONResponse({"ok": True, "message": "Everything deleted."})
    finally:
        db.close()


# =========================
# Twilio Webhooks: inbound SMS + recordings
# =========================
@app.post("/twilio/sms/inbound")
def twilio_sms_inbound(
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form("")
):
    db = SessionLocal()
    try:
        from_phone = normalize_phone(From) or From
        to_phone = normalize_phone(To) or To
        body = (Body or "").strip()

        lead = db.query(Lead).filter(Lead.phone == from_phone).first()
        if not lead:
            _log(db, None, None, "SMS_IN_UNKNOWN", f"From={from_phone} Body={body}")
            db.commit()
            try:
                fake = type("X", (), {"id": 0, "full_name": "Unknown Lead", "phone": from_phone})()
                notify_owner(db, fake, body, tag="LEAD_REPLIED_UNKNOWN")
            except Exception:
                pass
            return Response(content="<Response></Response>", media_type="text/xml")

        db.add(Message(
            lead_id=lead.id,
            direction="IN",
            channel="SMS",
            from_number=from_phone,
            to_number=to_phone,
            body=body,
            provider_sid=MessageSid or "",
            created_at=_now(),
        ))
        _log(db, lead.id, None, "SMS_IN", body)
        db.commit()

        # Always forward inbound reply to your phone
        notify_owner(db, lead, body, tag="LEAD_REPLIED")

        intent = classify_inbound_text(body)

        if intent == "STOP":
            lead.state = "DO_NOT_CONTACT"
            lead.updated_at = _now()
            cancel_pending_actions(db, lead.id, "Inbound STOP/DNC")
            _log(db, lead.id, None, "COMPLIANCE_DNC", "Lead opted out via SMS")
            db.commit()
            notify_owner(db, lead, "Lead opted out (STOP/DNC).", tag="DNC")
            return Response(content="<Response></Response>", media_type="text/xml")

        if intent in ["HOT", "QUESTION", "NEUTRAL", "APPT"]:
            # You asked for conservative behavior: tell you to call / take over
            lead.state = "CONTACTED" if intent == "HOT" else "WORKING"
            lead.updated_at = _now()
            db.commit()
            notify_owner(db, lead, f"Lead reply requires your attention ({intent}). Call them.", tag="CALL_THIS_LEAD")

        return Response(content="<Response></Response>", media_type="text/xml")
    finally:
        db.close()

@app.post("/twilio/recording")
def twilio_recording(
    RecordingSid: str = Form(...),
    RecordingUrl: str = Form(...),
    CallSid: str = Form(...)
):
    db = SessionLocal()
    try:
        playable = (RecordingUrl or "").strip()
        mp3 = playable + ".mp3" if playable and not playable.endswith(".mp3") else playable
        _log(db, None, None, "CALL_RECORDING", f"callSid={CallSid} recordingSid={RecordingSid} url={playable} mp3={mp3}")
        db.commit()
        return {"ok": True}
    finally:
        db.close()
