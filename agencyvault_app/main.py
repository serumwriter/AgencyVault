
# agencyvault_app/main.py
# AgencyVault - AI Employee Command Center (single-file, copy/paste)
# RULES ENFORCED:
# - Phone is mandatory for a Lead (imports skip rows without a valid phone)
# - Lead table stays minimal; ALL extra fields go into LeadMemory
# - CSV + PDF + Image imports normalize into one pipeline
# - No schema breakage from vendor fields (us_state/coverage/etc stored in LeadMemory)
# - Buttons: Text Now / Call Now (creates actions + tries to send immediately)
# - Worker endpoint executes PENDING actions with timezone + quiet-hours rules
# - Calendar panel: shows upcoming "appointment" memory entries (Google sync stub included)

import csv
import io
import json
import os
import re
print("### AGENDA DEBUG: main.py LOADED ###")
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from .database import engine, SessionLocal
from .models import Base, Lead, LeadMemory, Action, AgentRun, AuditLog, Message

# Twilio client functions (must exist in your codebase)
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


app = FastAPI(title="AgencyVault - AI Employee")


# =========================
# Startup / Schema
# =========================
@app.on_event("startup")
def _startup():
    Base.metadata.create_all(bind=engine)


# =========================
# Core helpers / sanitization
# =========================
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_US_STATE_RE = re.compile(
    r"^(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)$",
    re.I,
)

BAD_NAME_WORDS = {
    "lead", "bronze", "silver", "gold", "platinum", "ethos", "goat",
    "fresh", "aged", "new", "facebook", "insurance", "prospect", "unknown",
    "meta", "client", "customer", "applicant", "iul", "term", "whole", "life",
    "mortgage", "final", "expense", "annuity", "inquiry"
}

TIER_WORDS = {"bronze", "silver", "gold", "platinum", "fresh", "aged", "new", "goat", "ethos"}

PHONE_RE = re.compile(r"(\+?1?\s*\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

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

def normalize_state(val: Any) -> Optional[str]:
    s = (clean_text(val) or "").strip()
    if not s:
        return None
    s2 = s.upper()
    if _US_STATE_RE.match(s2):
        return s2
    return None

def safe_full_name(val: Any) -> str:
    s = clean_text(val) or ""
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "Unknown"
    low = s.lower()
    # If the "name" looks like a tier/source/keyword, treat as unknown
    if low in BAD_NAME_WORDS:
        return "Unknown"
    if any(x in low for x in ["bronze", "silver", "gold", "platinum", "fresh", "aged"]):
        # If it's basically just those words, not a real name
        parts = [p for p in re.split(r"[\s,]+", low) if p]
        if all(p in TIER_WORDS or p in BAD_NAME_WORDS for p in parts):
            return "Unknown"
    # If contains too many digits, it's not a name
    if sum(c.isdigit() for c in s) >= 2:
        return "Unknown"
    return s[:200]

def safe_first_name(full_name: Optional[str]) -> str:
    if not full_name:
        return ""
    first = (full_name.strip().split() or [""])[0].strip().lower()
    if not first or first in BAD_NAME_WORDS or len(first) < 2:
        return ""
    if any(c.isdigit() for c in first):
        return ""
    return first.capitalize()

def _log(db: Session, lead_id: Optional[int], run_id: Optional[int], event: str, detail: str):
    db.add(AuditLog(
        lead_id=lead_id,
        run_id=run_id,
        event=(event or "")[:120],
        detail=(detail or "")[:5000],
        created_at=_now(),
    ))

def mem_get(db: Session, lead_id: int, key: str) -> Optional[str]:
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def mem_set(db: Session, lead_id: int, key: str, value: str):
    v = (value or "").strip()
    if not v:
        return
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = v
        row.updated_at = _now()
    else:
        db.add(LeadMemory(lead_id=lead_id, key=key, value=v, updated_at=_now()))

def mem_bulk_set(db: Session, lead_id: int, d: Dict[str, Any]):
    for k, v in (d or {}).items():
        if v is None:
            continue
        vv = clean_text(v)
        if vv:
            mem_set(db, lead_id, k, vv)

def require_admin(req: Request, token_from_form: str = "") -> bool:
    token = (token_from_form or req.headers.get("x-admin-token", "") or req.query_params.get("token", "") or "").strip()
    want = (os.getenv("ADMIN_TOKEN") or "").strip()
    if not want:
        return False
    return token == want

def owner_mobile() -> str:
    return (os.getenv("OWNER_MOBILE") or os.getenv("ALERT_PHONE_NUMBER") or "").strip()

def notify_owner(db: Session, lead: Optional[Lead], msg: str, tag: str = "OWNER_NOTIFY"):
    who = ""
    if lead:
        who = f"#{lead.id} {lead.full_name or 'Unknown'} {lead.phone or ''}".strip()
    payload = f"{tag}\n{who}\n\n{(msg or '').strip()}".strip()
    try:
        _log(db, lead.id if lead else None, None, tag, payload[:5000])
        db.commit()
    except Exception:
        pass
    try:
        if owner_mobile():
            send_alert_sms(payload)
    except Exception as e:
        try:
            _log(db, lead.id if lead else None, None, "OWNER_NOTIFY_FAILED", str(e)[:500])
            db.commit()
        except Exception:
            pass

def dedupe_exists(db: Session, phone: Optional[str], email: Optional[str]) -> bool:
    if phone and db.query(Lead).filter(Lead.phone == phone).first():
        return True
    if email and db.query(Lead).filter(Lead.email == email).first():
        return True
    return False


# =========================
# Timezone inference (SAFE default + upgrade later)
# =========================
def infer_timezone_from_phone(phone_e164: Optional[str]) -> str:
    # Safe default for now; upgrade later with libphonenumber/area-code map.
    return (os.getenv("APP_TIMEZONE") or os.getenv("DEFAULT_TIMEZONE") or "America/Denver").strip()

def allowed_to_contact_now(tz_name: str) -> bool:
    tz_name = (tz_name or "").strip() or infer_timezone_from_phone(None)
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo(infer_timezone_from_phone(None))
    hr = datetime.now(tz).hour
    # Compliance window: 8am to 8:59pm
    return 8 <= hr < 21


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
        return "\n\n".join(chunks)
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
# Normalization: CSV rows + raw text blocks (PDF/OCR)
# =========================
def _looks_like_header(row: List[str]) -> bool:
    low = ",".join([(c or "").strip().lower() for c in row])
    return ("first" in low and "last" in low and "phone" in low) or ("email" in low and "phone" in low)

def normalize_csv_rows(rows: List[List[str]]) -> List[Dict[str, Any]]:
    """
    Your vendor positional CSV format (most common):
      0 First
      1 Last
      2 Product (IUL/TERM/etc)
      3 Tier/Vendor label (GOAT/ETHOS/FRESH/AGED/etc)
      4 Phone
      6 DOB
      7 Email
      8 State
    """
    out: List[Dict[str, Any]] = []
    if not rows:
        return out
    # If first row is header, skip
    start = 1 if isinstance(rows[0], list) and _looks_like_header(rows[0]) else 0

    for r in rows[start:]:
        if not isinstance(r, list):
            continue
        # Pad
        while len(r) < 9:
            r.append("")
        first = (r[0] or "").strip()
        last = (r[1] or "").strip()
        product = (r[2] or "").strip()
        tier = (r[3] or "").strip()
        phone = (r[4] or "").strip()
        dob = (r[6] or "").strip() if len(r) > 6 else ""
        email = (r[7] or "").strip() if len(r) > 7 else ""
        st = (r[8] or "").strip() if len(r) > 8 else ""

        full_name = safe_full_name(f"{first} {last}".strip())

        out.append({
            "full_name": full_name,
            "phone": phone,
            "email": email,
            "us_state": st,
            "birthdate": dob,
            "product_interest": product,
            "tier": tier,
            "lead_source": "csv_vendor",
        })
    return out

def _split_text_into_lead_blocks(raw: str) -> List[str]:
    """
    Prevents 'one lead becomes 100 leads' by splitting on strong separators only.
    If no separators are found, returns one block (and we then extract multiple phones/emails as extras).
    """
    t = (raw or "").strip()
    if not t:
        return []

    # Strong boundaries commonly present in lead PDFs
    boundary = re.compile(
        r"(?im)^\s*(inquiry\s*id|inquiry\s*Id|lead\s*id|lead\s*Id)\s*[:#]",
    )

    lines = t.splitlines()
    blocks: List[List[str]] = []
    cur: List[str] = []

    for line in lines:
        if boundary.search(line) and cur:
            blocks.append(cur)
            cur = [line]
        else:
            cur.append(line)

    if cur:
        blocks.append(cur)

    # If we only got 1 block, try alternative: long dashed separators
    if len(blocks) <= 1:
        alt = re.split(r"(?m)^\s*-{5,}\s*$|^\s*={5,}\s*$", t)
        alt = [a.strip() for a in alt if a.strip()]
        if len(alt) > 1:
            return alt

    return ["\n".join(b).strip() for b in blocks if b and "\n".join(b).strip()]

def _extract_contacts_from_block(block: str) -> Tuple[Optional[str], List[str], List[str]]:
    phones_raw = PHONE_RE.findall(block or "")
    emails = EMAIL_RE.findall(block or "")

    norm_phones: List[str] = []
    for pr in phones_raw:
        p = normalize_phone(pr)
        if p and p not in norm_phones:
            norm_phones.append(p)

    norm_emails: List[str] = []
    for e in emails:
        ee = clean_text(e)
        if ee and ee not in norm_emails:
            norm_emails.append(ee)

    primary = norm_phones[0] if norm_phones else None
    return primary, norm_phones, norm_emails

def _guess_name_from_block(block: str) -> str:
    # Look for "First Name:" / "Last Name:" / "Name:"
    first = None
    last = None
    m = re.search(r"(?im)^\s*First\s*Name\s*:\s*(.+)\s*$", block)
    if m:
        first = clean_text(m.group(1))
    m = re.search(r"(?im)^\s*Last\s*Name\s*:\s*(.+)\s*$", block)
    if m:
        last = clean_text(m.group(1))
    if first or last:
        return safe_full_name(f"{first or ''} {last or ''}".strip())

    m = re.search(r"(?im)^\s*Name\s*:\s*(.+)\s*$", block)
    if m:
        return safe_full_name(m.group(1))

    # Fallback: first non-empty line that looks like a person name
    for line in (block or "").splitlines():
        s = clean_text(line)
        if not s:
            continue
        if len(s) > 45:
            continue
        low = s.lower()
        # Skip obvious non-name lines
        if any(w in low for w in ["inquiry", "coverage", "amount", "address", "city", "state", "zip", "phone", "email"]):
            continue
        if sum(c.isdigit() for c in s) >= 1:
            continue
        parts = s.split()
        if len(parts) >= 2 and all(len(p) >= 2 for p in parts[:2]):
            nm = safe_full_name(s)
            if nm != "Unknown":
                return nm
    return "Unknown"

def normalize_text_to_leads(raw: str) -> List[Dict[str, Any]]:
    blocks = _split_text_into_lead_blocks(raw)
    out: List[Dict[str, Any]] = []

    for b in blocks:
        primary_phone, phones, emails = _extract_contacts_from_block(b)
        # Phone is mandatory (your rule)
        if not primary_phone:
            continue

        name = _guess_name_from_block(b)

        # Extract common fields if present
        tier = None
        m = re.search(r"(?im)\b(BRONZE|SILVER|GOLD|PLATINUM|FRESH|AGED|GOAT|ETHOS)\b", b)
        if m:
            tier = m.group(1).upper()

        us_state = None
        m = re.search(r"(?im)^\s*State\s*:\s*([A-Za-z]{2})\s*$", b)
        if m:
            us_state = normalize_state(m.group(1))

        dob = None
        m = re.search(r"(?im)^\s*(DOB|Date of Birth|Birthdate)\s*:\s*(.+)\s*$", b)
        if m:
            dob = clean_text(m.group(2))

        cov = None
        m = re.search(r"(?im)^\s*(Requested Coverage|Coverage Amount|Face Value|Current Coverage Amount)\s*:\s*([$]?\s*[\d,]+)\s*$", b)
        if m:
            cov = clean_text(m.group(2))

        inquiry_id = None
        m = re.search(r"(?im)^\s*(Inquiry\s*Id|Inquiry\s*ID|Lead\s*Id|Lead\s*ID)\s*[:#]\s*(.+)\s*$", b)
        if m:
            inquiry_id = clean_text(m.group(2))

        out.append({
            "full_name": name,
            "phone": primary_phone,
            "email": (emails[0] if emails else None),
            "tier": tier,
            "us_state": us_state,
            "birthdate": dob,
            "coverage_requested": cov,
            "lead_reference": inquiry_id,
            "phones_all": phones,
            "emails_all": emails,
            "raw_text": b[:12000],
            "lead_source": "pdf_or_ocr",
        })

    return out


# =========================
# Import helper: insert/merge safely (NO extra Lead columns)
# =========================
def import_one_lead(db: Session, item: Dict[str, Any], source_tag: str) -> Dict[str, Any]:
    """
    Returns: {"ok": bool, "created": bool, "merged": bool, "skipped": bool, "reason": "...", "lead_id": int|None}
    Enforces: phone mandatory
    """
    phone = normalize_phone(item.get("phone") or "")
    if not phone:
        return {"ok": True, "skipped": True, "reason": "missing_phone", "lead_id": None}

    email = clean_text(item.get("email") or "")
    full_name = safe_full_name(item.get("full_name"))

    existing = db.query(Lead).filter(Lead.phone == phone).first()
    if existing:
        # Merge extras to memory
        extras = dict(item)
        extras.pop("phone", None)
        extras.pop("email", None)
        extras.pop("full_name", None)

        mem_set(db, existing.id, "source_tag", source_tag)
        mem_set(db, existing.id, "source_type", source_tag)
        mem_bulk_set(db, existing.id, extras)
        existing.updated_at = _now()
        return {"ok": True, "created": False, "merged": True, "skipped": False, "lead_id": existing.id}

    tz = infer_timezone_from_phone(phone)

    lead = Lead(
        full_name=full_name or "Unknown",
        phone=phone,                  # MANDATORY
        email=email or None,
        state="NEW",                  # workflow state
        timezone=tz,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(lead)
    db.flush()

    # Everything else into memory
    extras = dict(item)
    extras.pop("phone", None)
    extras.pop("email", None)
    extras.pop("full_name", None)

    mem_set(db, lead.id, "source_tag", source_tag)
    mem_set(db, lead.id, "source_type", source_tag)
    mem_bulk_set(db, lead.id, extras)

    return {"ok": True, "created": True, "merged": False, "skipped": False, "lead_id": lead.id}


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
# Worker execution (PENDING actions)
# =========================
def execute_pending_actions(db: Session, limit: int = 5) -> Dict[str, Any]:
    nowv = _now()
    executed = 0
    failed = 0
    skipped = 0

    actions = (
        db.query(Action)
        .filter(Action.status == "PENDING")
        .order_by(Action.created_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
        .all()
    )

    for a in actions:
        try:
            payload = json.loads(a.payload_json or "{}")

            lead = db.query(Lead).filter_by(id=a.lead_id).first()
            if not lead:
                a.status = "FAILED"
                a.error = "Lead missing"
                failed += 1
                continue

            # Due time (for calls)
            due_at = payload.get("due_at")
            if due_at:
                try:
                    if nowv < datetime.fromisoformat(due_at):
                        skipped += 1
                        continue
                except Exception:
                    pass

            # Quiet hours for outreach (but NOT for owner alerts)
            tz_name = lead.timezone or infer_timezone_from_phone(lead.phone)
            if not allowed_to_contact_now(tz_name):
                skipped += 1
                continue

            if a.type == "TEXT":
                send_lead_sms(payload["to"], payload["message"])
            elif a.type == "CALL":
                if not _make_call:
                    raise RuntimeError("Call function not available in twilio_client.py")
                _make_call(payload["to"], payload.get("lead_id"))
            else:
                raise RuntimeError(f"Unknown action type: {a.type}")

            a.status = "DONE"
            a.finished_at = _now()
            executed += 1

        except Exception as e:
            a.status = "FAILED"
            a.error = str(e)[:500]
            failed += 1

    db.commit()
    return {"ok": True, "executed": executed, "failed": failed, "skipped": skipped}

# Allow GET so you can click it in browser (you hit 405 earlier)
@app.get("/worker/execute")
def worker_execute(limit: int = 5):
    db = SessionLocal()
    try:
        out = execute_pending_actions(db, limit=limit)
        _log(db, None, None, "WORKER_EXECUTE", json.dumps(out)[:5000])
        db.commit()
        return out
    finally:
        db.close()

def render_action(a):
    l = leads.get(a.lead_id)
    p = _parse_payload(a)
    phone = (l.phone if l else "") or "-"
    name = (l.full_name if l else "") or f"Lead #{a.lead_id}"
    msg = p.get("message") if a.type == "TEXT" else ""
    when = p.get("when") or ""
    reason = p.get("reason") or ""

    if a.type == "CALL":
        todo = f"CALL this person manually: {phone}"
    elif a.type == "TEXT":
        todo = f"TEXT this person manually: {phone}"
    else:
        todo = f"APPOINTMENT: {when}"

    msg_html = ""
    if msg:
        msg_html = f'<div class="muted" style="margin-top:6px;white-space:pre-wrap">{msg}</div>'

    return f"""
    <div class="item">
      <div class="top">
        <div class="name"><a href="/leads/{a.lead_id}">#{a.lead_id} {name}</a></div>
        <div class="tag">{a.type}</div>
      </div>

      <div class="muted"><b>DO THIS:</b> {todo}</div>
      <div class="muted">Reason: {reason or "-"}</div>
      {msg_html}

      <form method="post" action="/agenda/report" style="margin-top:10px">
        <input type="hidden" name="action_id" value="{a.id}" />

        <textarea name="note" placeholder="Paste what they said or write quick notes (AI will decide next step)"
          style="width:100%;min-height:70px;margin-top:6px"></textarea>

        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
          <button class="btn" name="outcome" value="talked">Talked / Replied</button>
          <button class="btn" name="outcome" value="no_answer">No answer</button>
          <button class="btn" name="outcome" value="not_interested">Not interested</button>
          <button class="btn" name="outcome" value="booked">Booked (verbal yes)</button>
        </div>
      </form>
    </div>
    """

# =========================
# Planner (creates PENDING actions)
# =========================
def ai_schedule_appointment(db, lead_id: int, note: str = "Call"):
    """
    AI-only scheduler.
    Finds the next open 30-minute slot and books it.
    """

    # 1. Get existing appointments
    appts = (
        db.query(Action)
        .filter(Action.kind == "APPOINTMENT")
        .order_by(Action.created_at.asc())
        .all()
    )

    # 2. Build blocked times (simple version)
    blocked = []
    for a in appts:
        payload = a.payload or {}
        when = payload.get("when")
        if when:
            blocked.append(when)

    # 3. Pick next available slot (VERY SAFE DEFAULT)
    from datetime import datetime, timedelta

    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    candidate = now + timedelta(hours=1)

    for _ in range(48):  # look ahead ~2 days
        slot = candidate.strftime("%Y-%m-%d %H:%M")
        if slot not in blocked:
            break
        candidate += timedelta(minutes=30)

    # 4. Create appointment
    create_task(
        db,
        kind="APPOINTMENT",
        lead_id=lead_id,
        payload={
            "when": slot,
            "note": note,
            "tz": "local",
            "name": "AI Scheduled Call",
        },
    )

    log_event(db, "ai_schedule", f"AI scheduled call for lead {lead_id} at {slot}")

def plan_actions(db: Session, batch_size: int = 25) -> Dict[str, Any]:
    run = AgentRun(mode="planning", status="STARTED", batch_size=batch_size, notes="Planner run")
    db.add(run)
    db.flush()

    planned = 0
    considered = 0
    nowv = _now()

    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW")
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        considered += 1

        if not (lead.phone or "").strip():
            continue

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
            payload_json=json.dumps({"to": lead.phone, "message": msg1}),
            created_at=nowv,
        ))
        planned += 1

        call_due = (nowv + timedelta(minutes=15)).isoformat()
        db.add(Action(
            lead_id=lead.id,
            type="CALL",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({"to": lead.phone, "lead_id": lead.id, "due_at": call_due}),
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

@app.get("/agenda", response_class=HTMLResponse)
def agenda():
    db = SessionLocal()
    try:
        actions = (
            db.query(Action, Lead)
            .join(Lead, Lead.id == Action.lead_id)
            .filter(Action.status == "PENDING")
            .order_by(Action.created_at.asc())
            .limit(1)
            .all()
        )

        if not actions:
            body = "<p>No tasks right now. Click Start My Workday.</p>"
        else:
            a, l = actions[0]

            payload = json.loads(a.payload_json or "{}")
            due = payload.get("due_at")
            reason = payload.get("reason", "AI decided this is next")

            when = "Do now"
            if due:
                when = f"Scheduled for {due}"

            body = f"""
            <h2>Next Task</h2>

            <div style="margin-top:10px">
              <b>Lead:</b> {l.full_name or "Unknown"}<br>
              <b>Phone:</b> {l.phone}<br>
              <b>Status:</b> {l.state}
            </div>

            <div style="margin-top:10px">
              <b>Action:</b> {a.type}<br>
              <b>When:</b> {when}<br>
              <b>Why:</b> {reason}
            </div>

            <form method="post" action="/agenda/report" style="margin-top:14px">
              <input type="hidden" name="action_id" value="{a.id}" />

              <textarea name="note"
                placeholder="Paste what the lead said, or what happened"
                style="width:100%;min-height:80px;margin-top:10px"></textarea>

              <div style="margin-top:10px">
                <button type="submit" name="outcome" value="talked">Talked / Replied</button>
                <button type="submit" name="outcome" value="no_answer">No Answer</button>
                <button type="submit" name="outcome" value="not_interested">Not Interested</button>
                <button type="submit" name="outcome" value="booked">Booked</button>
              </div>
            </form>
            """

        return HTMLResponse(f"""
        <html>
        <head>
          <title>Agenda</title>
        </head>
        <body style="background:#111;color:#eee;font-family:Arial;padding:20px">
          <h1>AI Agenda (What to do now)</h1>
          <p>This page tells you exactly what to do. Complete items top to bottom.</p>
          {body}
        </body>
        </html>
        """)
    finally:
        db.close()

@app.post("/workday/start")
def start_workday():
    """
    Enterprise mode:
    - AI decides what to do
    - Plans a full day safely
    - Sends user straight to execution
    """
    db = SessionLocal()
    try:
        # Let AI decide the workload (enterprise default)
        plan_actions(db, batch_size=120)
        db.commit()
    finally:
        db.close()

    # Send the user straight to work
    return RedirectResponse("/agenda", status_code=303)

# =========================
# Dashboard UI helpers
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

def _fmt_dt(s: str) -> str:
    try:
        return datetime.fromisoformat(s).strftime("%b %d %I:%M %p")
    except Exception:
        return s[:40]

def _upcoming_appts(db, limit=10):
    rows = (
        db.query(Action)
        .filter(Action.kind == "APPOINTMENT")
        .order_by(Action.created_at.desc())
        .limit(limit)
        .all()
    )

    out = []
    for r in rows:
        payload = r.payload or {}
        out.append({
            "lead_id": r.lead_id or 0,
            "name": payload.get("name") or "Appointment",
            "when": payload.get("when") or "Unknown time",
            "tz": payload.get("tz") or "local",
            "note": payload.get("note") or "",
        })
    return out


# =========================
# Dashboard (mobile-safe, no sideways scroll)
# =========================
from sqlalchemy import or_

def _get_mem_map(db: Session, lead_ids: List[int]) -> Dict[int, Dict[str, str]]:
    """
    Fetch LeadMemory for a set of leads in one query.
    Returns {lead_id: {key: value}}
    """
    if not lead_ids:
        return {}
    rows = db.query(LeadMemory).filter(LeadMemory.lead_id.in_(lead_ids)).all()
    out: Dict[int, Dict[str, str]] = {}
    for r in rows:
        out.setdefault(r.lead_id, {})[r.key] = r.value
    return out

def _upcoming_appts(db: Session, limit: int = 8) -> List[Dict[str, Any]]:
    """
    Simple local "calendar" until Google Calendar sync is enabled.
    Convention: LeadMemory key 'appt_time' = ISO string, and optional 'appt_note'.
    """
    # This is intentionally conservative and won't crash if no rows exist.
    rows = (
        db.query(LeadMemory)
        .filter(LeadMemory.key == "appt_time")
        .order_by(LeadMemory.updated_at.desc().nullslast())
        .limit(200)
        .all()
    )
    items = []
    for r in rows:
        try:
            lead = db.query(Lead).filter(Lead.id == r.lead_id).first()
            if not lead:
                continue
            note = mem_get(db, lead.id, "appt_note") or ""
            tz = lead.timezone or (os.getenv("DEFAULT_TIMEZONE") or "America/Denver")
            when = (r.value or "").strip()
            if not when:
                continue
            items.append({
                "lead_id": lead.id,
                "name": lead.full_name or "Unknown",
                "when": when,
                "tz": tz,
                "note": note[:180],
            })
        except Exception:
            continue
        if len(items) >= limit:
            break
    return items

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

        # Activity feed (safe + limited)
        logs = db.query(AuditLog).order_by(AuditLog.created_at.desc()).limit(18).all()
        feed = ""
        for l in logs:
            feed += f"""
            <div class="feed-item">
              <div class="feed-top">
                <div class="feed-title">{(l.event or "")}</div>
                <div class="feed-time">{str(l.created_at)[:19]}</div>
              </div>
              <div class="feed-meta">lead={l.lead_id} run={l.run_id}</div>
              <div class="feed-body">{(l.detail or "")[:280]}</div>
            </div>
            """

        # Newest leads
        leads = db.query(Lead).order_by(Lead.created_at.desc()).limit(12).all()
        lead_ids = [x.id for x in leads]
        mem_map = _get_mem_map(db, lead_ids)

        leads_html = ""
        for l in leads:
            mem = mem_map.get(l.id, {})
            us_state = mem.get("us_state") or mem.get("state") or "-"
            cov = mem.get("coverage_requested") or mem.get("coverage") or "-"
            tier = mem.get("tier") or "-"
            prod = mem.get("product_interest") or mem.get("coverage_type") or "-"
            leads_html += f"""
            <div class="lead-row">
              <div class="lead-main">
                <div class="lead-name"><a href="/leads/{l.id}">#{l.id} {l.full_name or "Unknown"}</a></div>
                <div class="lead-meta">{l.phone or "-"} | {l.email or "-"}</div>
                <div class="lead-meta">Tier: {tier} | Product: {prod} | US: {us_state} | Coverage: {cov}</div>
              </div>
              <div class="lead-actions">
                <form method="post" action="/leads/{l.id}/text-now" style="margin:0">
                  <button class="mini" type="submit">Text Now</button>
                </form>
                <form method="post" action="/leads/{l.id}/call-now" style="margin:0">
                  <button class="mini" type="submit">Call Now</button>
                </form>
                <span class="pill">{l.state}</span>
              </div>
            </div>
            """

        denom = max(total, 1)
        pct_new = (new / denom) * 100.0
        pct_working = (working / denom) * 100.0
        pct_contacted = (contacted / denom) * 100.0
        pct_dnc = (dnc / denom) * 100.0

        # Calendar panel (local memory now, Google sync later)
        appts = _upcoming_appts(db, limit=8)
        appt_html = ""
        for a in appts:
            appt_html += f"""
            <div class="appt">
              <div class="appt-top">
                <div class="appt-title"><a href="/leads/{a["lead_id"]}">#{a["lead_id"]} {a["name"]}</a></div>
                <div class="appt-when">{a["when"]} ({a["tz"]})</div>
              </div>
              <div class="appt-note">{a["note"] or ""}</div>
            </div>
            """
        if not appt_html:
            appt_html = '<div class="muted">No appointments stored yet. Google Calendar sync will appear here.</div>'

        pause_label = "Paused" if paused else "Running"

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AgencyVault - AI Employee</title>
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
  * {{ box-sizing:border-box; }}
  body {{
    margin:0;
    background:var(--bg);
    color:var(--text);
    font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;
    overflow-x:hidden; /* prevents sideways scroll */
  }}
  a {{ color:var(--link); text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}

  .wrap {{
    display:grid;
    grid-template-columns: 260px 1fr;
    min-height:100vh;
    width:100%;
  }}
  .sidebar {{
    border-right:1px solid var(--border);
    padding:16px 14px;
    position:sticky;
    top:0;
    height:100vh;
    overflow:auto;
    background:linear-gradient(180deg, rgba(17,24,39,.55), rgba(11,15,23,.55));
  }}
  .brand {{ font-weight:900; font-size:20px; margin-bottom:6px; }}
  .subtitle {{ color:var(--muted); font-size:13px; }}
  .nav {{ display:flex; flex-direction:column; gap:8px; margin-top:14px; }}
  .nav a {{
    display:block;
    padding:10px 12px;
    border-radius:12px;
    border:1px solid rgba(50,74,110,.15);
    background:rgba(15,22,36,.55);
  }}
  .nav a:hover {{ border-color:rgba(138,180,248,.55); }}

  .main {{
    padding:18px;
    width:100%;
    max-width:1250px;
  }}

  .topbar {{
    display:flex;
    justify-content:space-between;
    align-items:flex-end;
    gap:14px;
    flex-wrap:wrap;
    margin-bottom:14px;
  }}
  .title {{ font-size:26px; font-weight:900; }}
  .sub {{ color:var(--muted); font-size:13px; }}

  .kpis {{
    display:grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    gap:10px;
    margin-top:12px;
  }}
  .kpi {{
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:16px;
    padding:12px;
    min-width:0;
  }}
  .kpi-label {{ color:var(--muted); font-size:12px; }}
  .kpi-value {{ font-size:22px; font-weight:900; margin-top:2px; }}
  .kpi-sub {{ color:var(--muted); font-size:12px; margin-top:4px; }}

  .grid {{
    display:grid;
    grid-template-columns: 1fr 1fr;
    gap:12px;
    margin-top:12px;
  }}
  .panel {{
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:18px;
    padding:14px;
    min-width:0;
  }}
  .panel h2 {{ margin:0 0 10px 0; font-size:16px; font-weight:900; letter-spacing:.2px; }}

  .donuts {{
    display:grid;
    grid-template-columns: repeat(2, minmax(0,1fr));
    gap:10px;
  }}
  .donut-card {{
    background:var(--panel2);
    border:1px solid var(--border);
    border-radius:16px;
    padding:12px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    min-width:0;
  }}

  .feed-item {{ padding:10px 0; border-bottom:1px solid var(--border); }}
  .feed-top {{ display:flex; justify-content:space-between; align-items:center; gap:10px; }}
  .feed-title {{ font-weight:800; }}
  .feed-time {{ color:var(--muted); font-size:12px; }}
  .feed-meta {{ color:var(--muted); font-size:12px; margin-top:2px; }}
  .feed-body {{ margin-top:6px; color:rgba(230,237,243,.9); }}

  .lead-row {{
    display:flex;
    justify-content:space-between;
    align-items:flex-start;
    gap:12px;
    padding:10px 0;
    border-bottom:1px solid var(--border);
  }}
  .lead-main {{ min-width:0; }}
  .lead-name {{ font-weight:900; }}
  .lead-meta {{
    color:var(--muted);
    font-size:12px;
    margin-top:2px;
    word-break:break-word;
  }}
  .lead-actions {{
    display:flex;
    align-items:center;
    gap:8px;
    flex-wrap:wrap;
    justify-content:flex-end;
  }}
  .pill {{
    padding:3px 9px;
    border-radius:999px;
    border:1px solid rgba(138,180,248,.25);
    background:rgba(17,24,39,.6);
    font-size:12px;
    color:rgba(230,237,243,.9);
  }}

  .btn, .mini {{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    gap:8px;
    border-radius:12px;
    border:1px solid rgba(50,74,110,.35);
    background:rgba(17,24,39,.75);
    color:var(--text);
    cursor:pointer;
    font-weight:900;
  }}
  .btn {{ padding:10px 12px; }}
  .mini {{ padding:7px 10px; border-radius:10px; font-size:12px; }}

  textarea {{
    width:100%;
    background:rgba(11,15,23,.75);
    color:var(--text);
    border:1px solid rgba(50,74,110,.35);
    border-radius:14px;
    padding:12px;
    min-height:110px;
    font-size:14px;
    outline:none;
  }}
  pre {{
    white-space:pre-wrap;
    margin:10px 0 0 0;
    color:rgba(230,237,243,.9);
    font-size:13px;
  }}
  input {{
    width:100%;
    background:rgba(11,15,23,.75);
    color:var(--text);
    border:1px solid rgba(50,74,110,.35);
    border-radius:12px;
    padding:10px;
    outline:none;
  }}
  .muted {{ color:var(--muted); font-size:12px; }}

  .appt {{ padding:10px 0; border-bottom:1px solid rgba(50,74,110,.22); }}
  .appt-top {{ display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; }}
  .appt-title {{ font-weight:900; }}
  .appt-when {{ color:var(--muted); font-size:12px; }}
  .appt-note {{ margin-top:6px; color:rgba(230,237,243,.9); font-size:13px; }}

  @media (max-width: 1100px) {{
    .wrap {{ grid-template-columns: 1fr; }}
    .sidebar {{ position:relative; height:auto; border-right:none; }}
    .kpis {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    .grid {{ grid-template-columns: 1fr; }}
    .main {{ max-width:100%; }}
  }}
</style>
</head>
<body>

<div class="wrap">
  <aside class="sidebar">
    <div class="brand">AgencyVault</div>
    <div class="subtitle">AI Employee for Life Insurance</div>

    <div class="nav">
      <a href="/dashboard">Dashboard</a>
      <a href="/leads">All Leads</a>
      <a href="/leads/new">Add Lead</a>
      <a href="/actions">Action Queue</a>
      <a href="/activity">Activity Log</a>
      <a href="/ai/plan">Run Planner</a>
      <a href="/worker/execute?limit=5">Execute Now</a>
    </div>

    <div style="margin-top:14px" class="muted">
      Status: <b>{pause_label}</b><br>
      Phone is mandatory. Imports + outreach are designed to not crash.
    </div>
  </aside>
  <form method="post" action="/workday/start" style="margin:0">
  <button class="btn" type="submit">Start My Workday</button>
</form>

  <main class="main">
    <div class="topbar">
      <div>
        <div class="title">AI Employee Command Center</div>
        <div class="sub">You take the reins whenever you want. It runs the machine.</div>
      </div>
      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <a class="btn" href="/leads/new">Add Lead</a>
        <a class="btn" href="/leads">Browse Leads</a>
        <a class="btn" href="/worker/execute?limit=5">Execute Now</a>
      </div>
    </div>

    <div class="kpis">
      <div class="kpi"><div class="kpi-label">Total</div><div class="kpi-value">{total}</div><div class="kpi-sub">All time</div></div>
      <div class="kpi"><div class="kpi-label">NEW</div><div class="kpi-value">{new}</div><div class="kpi-sub">Not touched</div></div>
      <div class="kpi"><div class="kpi-label">WORKING</div><div class="kpi-value">{working}</div><div class="kpi-sub">Outreach running</div></div>
      <div class="kpi"><div class="kpi-label">CONTACTED</div><div class="kpi-value">{contacted}</div><div class="kpi-sub">Replied / progressed</div></div>
      <div class="kpi"><div class="kpi-label">DNC</div><div class="kpi-value">{dnc}</div><div class="kpi-sub">Compliance</div></div>
      <div class="kpi"><div class="kpi-label">Pending</div><div class="kpi-value">{pending}</div><div class="kpi-sub">Queued actions</div></div>
    </div>


      <div class="panel">
        <h2>Imports</h2>

        <div class="muted" style="margin-bottom:8px;">Upload CSV</div>
        <form action="/import/csv" method="post" enctype="multipart/form-data">
          <input type="file" name="file" accept=".csv" />
          <div style="margin-top:8px;"><button class="btn" type="submit">Upload CSV</button></div>
        </form>

        <div class="muted" style="margin:14px 0 8px;">Upload PDF</div>
        <form action="/import/pdf" method="post" enctype="multipart/form-data">
          <input type="file" name="file" accept=".pdf,application/pdf" />
          <div style="margin-top:8px;"><button class="btn" type="submit">Upload PDF</button></div>
        </form>

        <div class="muted" style="margin:14px 0 8px;">Upload Image</div>
        <form action="/import/image" method="post" enctype="multipart/form-data">
          <input type="file" name="file" accept="image/*" />
          <div style="margin-top:8px;"><button class="btn" type="submit">Upload Image</button></div>
        </form>

        <div class="muted" style="margin-top:12px;">
          If Twilio is blocked (10DLC), actions will show FAILED with the real error. Nothing will crash.
        </div>
      </div>

      <div class="panel">
        <h2>Newest Leads (Text/Call)</h2>
        <div>{leads_html or '<div class="muted">No leads yet.</div>'}</div>
      </div>

     <div class="grid">

  <div class="panel">
    <h2>Distribution</h2>
    <div class="muted">
      NEW: {new} ({pct_new:.1f}%)<br>
      WORKING: {working} ({pct_working:.1f}%)<br>
      CONTACTED: {contacted} ({pct_contacted:.1f}%)<br>
      DNC: {dnc} ({pct_dnc:.1f}%)
    </div>
  </div>

  <div class="panel">
    <h2>Calendar</h2>
    {appt_html}
    <div style="margin-top:10px" class="muted">
      Google Calendar sync will go here. Once connected, AI will not schedule over existing blocks.
    </div>
  </div>



      <div class="panel">
        <h2>Live Activity Feed</h2>
        <div>{feed or '<div class="muted">No activity yet.</div>'}</div>
      </div>

      <div class="panel">
        <h2>AI Employee</h2>
        <div class="muted">Commands: counts | run planner | execute | lead 123</div>
        <textarea id="cmd" placeholder="Try: counts"></textarea>
        <div style="display:flex; gap:10px; margin-top:10px; flex-wrap:wrap;">
          <button class="btn" onclick="sendCmd()">Send</button>
          <button class="btn" onclick="preset('counts')">Counts</button>
          <button class="btn" onclick="preset('run planner')">Run Planner</button>
          <button class="btn" onclick="preset('execute')">Execute</button>
        </div>
        <pre id="out" class="muted"></pre>

        <div style="margin-top:10px" class="muted">
          Live call + transcript requires Twilio Media Streams + a websocket service.
          Dashboard is ready for it; we wire that next.
        </div>
      </div>
    </div>
  </main>
</div>

<script>
function preset(v) {{
  document.getElementById("cmd").value = v;
  sendCmd();
}}

async function sendCmd() {{
  const msg = document.getElementById("cmd").value;
  const out = document.getElementById("out");
  out.textContent = "Working...";
  try {{
    const r = await fetch("/api/assistant", {{
      method: "POST",
      headers: {{ "Content-Type": "application/json" }},
      body: JSON.stringify({{ message: msg }})
    }});
    const d = await r.json();
    out.textContent = d.reply || "OK";
  }} catch (e) {{
    out.textContent = "Error talking to assistant.";
  }}
}}
</script>

</body>
</html>
        """)
    finally:
        db.close()

@app.post("/leads/{lead_id}/text-now")
def text_now(lead_id: int):
    """
    Immediate operator-triggered text:
    Creates a PENDING TEXT action so the worker executes it (no blocking).
    """
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead or not (lead.phone or "").strip():
            return RedirectResponse("/dashboard", status_code=303)

        first = safe_first_name(lead.full_name)
        msg = (
            f"Hi{(' ' + first) if first else ''}, this is Nick's office. "
            "You requested life insurance info — want a quick quote today?"
        )

        db.add(Action(
            lead_id=lead.id,
            type="TEXT",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({"to": lead.phone, "message": msg}),
            created_at=_now(),
        ))
        _log(db, lead.id, None, "TEXT_NOW_QUEUED", msg[:400])
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.post("/leads/{lead_id}/call-now")
def call_now(lead_id: int):
    """
    Immediate operator-triggered call:
    Creates a PENDING CALL action so the worker executes it (no blocking).
    """
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead or not (lead.phone or "").strip():
            return RedirectResponse("/dashboard", status_code=303)

        db.add(Action(
            lead_id=lead.id,
            type="CALL",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({"to": lead.phone, "lead_id": lead.id, "due_at": _now().isoformat()}),
            created_at=_now(),
        ))
        _log(db, lead.id, None, "CALL_NOW_QUEUED", f"to={lead.phone}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

# =========================
# Imports (ONE route each)
# =========================
@app.post("/import/csv")
async def import_csv(file: UploadFile = File(...)):
    db = SessionLocal()
    try:
        content = await file.read()
        if not content:
            return RedirectResponse("/dashboard", status_code=303)

        text_data = content.decode("utf-8", errors="ignore")
        reader = csv.reader(io.StringIO(text_data))
        rows = list(reader)

        items = normalize_csv_rows(rows)

        created = 0
        merged = 0
        skipped = 0

        for it in items:
            res = import_one_lead(db, it, source_tag="csv")
            if res.get("skipped"):
                skipped += 1
            elif res.get("merged"):
                merged += 1
            elif res.get("created"):
                created += 1

        _log(db, None, None, "IMPORT_CSV", f"created={created} merged={merged} skipped={skipped} rows={len(rows)}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.post("/import/pdf")
async def import_pdf(file: UploadFile = File(...)):
    if not PDF_OK:
        return HTMLResponse("PDF support not installed (pypdf missing).", status_code=400)

    db = SessionLocal()
    try:
        data = await file.read()
        if not data:
            return HTMLResponse("Empty upload", status_code=400)

        text_data = extract_text_from_pdf_bytes(data)
        if not (text_data or "").strip():
            _log(db, None, None, "IMPORT_PDF_EMPTY", "No readable PDF text")
            db.commit()
            return RedirectResponse("/dashboard", status_code=303)

        items = normalize_text_to_leads(text_data)

        created = 0
        merged = 0
        skipped = 0

        for it in items:
            # Always store source filename/page context when possible
            it["source_filename"] = clean_text(file.filename or "uploaded.pdf")
            res = import_one_lead(db, it, source_tag="pdf")
            if res.get("skipped"):
                skipped += 1
            elif res.get("merged"):
                merged += 1
            elif res.get("created"):
                created += 1

        _log(db, None, None, "IMPORT_PDF", f"created={created} merged={merged} skipped={skipped} blocks={len(items)}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()

@app.post("/import/image")
async def import_image(file: UploadFile = File(...)):
    if not OCR_OK:
        return HTMLResponse("OCR not installed (pytesseract/PIL missing).", status_code=400)

    db = SessionLocal()
    try:
        data = await file.read()
        if not data:
            return HTMLResponse("Empty upload", status_code=400)

        text_data = extract_text_from_image_bytes(data)
        if not (text_data or "").strip():
            _log(db, None, None, "IMPORT_IMAGE_EMPTY", "No readable OCR text")
            db.commit()
            return RedirectResponse("/dashboard", status_code=303)

        items = normalize_text_to_leads(text_data)

        created = 0
        merged = 0
        skipped = 0

        for it in items:
            it["source_filename"] = clean_text(file.filename or "uploaded_image")
            res = import_one_lead(db, it, source_tag="image")
            if res.get("skipped"):
                skipped += 1
            elif res.get("merged"):
                merged += 1
            elif res.get("created"):
                created += 1

        _log(db, None, None, "IMPORT_IMAGE", f"created={created} merged={merged} skipped={skipped} blocks={len(items)}")
        db.commit()
        return RedirectResponse("/dashboard", status_code=303)
    finally:
        db.close()


# =========================
# Leads: Add + List + Detail + Delete
# =========================
@app.get("/leads/new", response_class=HTMLResponse)
def leads_new_form():
    return HTMLResponse("""
    <html><body style="font-family:system-ui;padding:24px;background:#0b0f17;color:#e6edf3;max-width:900px;margin:0 auto;">
      <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;">Back</a>
      <h2 style="margin-top:12px;">Add Lead</h2>
      <form method="post" action="/leads/new" style="margin-top:14px;">
        <div style="opacity:.8">Full Name</div>
        <input name="full_name" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
        <br><br>
        <div style="opacity:.8">Phone (required)</div>
        <input name="phone" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
        <br><br>
        <div style="opacity:.8">Email</div>
        <input name="email" style="width:100%;padding:10px;border-radius:10px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
        <br><br>
        <button type="submit" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
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
        if not p:
            return HTMLResponse("<div style='color:#ffb4b4;font-family:system-ui;padding:20px'>Invalid phone (required).</div>", status_code=400)

        e = clean_text(email)
        n = safe_full_name(full_name)

        if dedupe_exists(db, p, e):
            return HTMLResponse("<div style='color:#ffb4b4;font-family:system-ui;padding:20px'>Duplicate lead.</div>", status_code=409)

        tz = infer_timezone_from_phone(p)

        lead = Lead(
            full_name=n,
            phone=p,
            email=e or None,
            state="NEW",
            timezone=tz,
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(lead)
        db.flush()

        mem_set(db, lead.id, "source_tag", "manual")
        _log(db, lead.id, None, "LEAD_CREATED", f"{n} {p}")
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
            mem = {m.key: m.value for m in db.query(LeadMemory).filter(LeadMemory.lead_id == l.id).all()}
            us_state = mem.get("us_state") or "-"
            cov = mem.get("coverage_requested") or "-"
            tier = mem.get("tier") or "-"
            rows += f"""
            <div style="padding:12px 0;border-bottom:1px solid rgba(50,74,110,.25);display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
              <div style="min-width:260px;max-width:760px;">
                <div style="font-weight:900;"><a href="/leads/{l.id}">#{l.id} {l.full_name or "Unknown"}</a> <span style="opacity:.75;font-weight:800;">[{l.state}]</span></div>
                <div style="opacity:.75;font-size:13px;margin-top:2px;">Phone: {l.phone or "-"} | Email: {l.email or "-"}</div>
                <div style="opacity:.75;font-size:13px;margin-top:2px;">Tier: {tier} | US State: {us_state} | Coverage: {cov}</div>
              </div>
              <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
                <form method="post" action="/leads/{l.id}/text-now" style="margin:0">
                  <button type="submit" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:8px 12px;border-radius:12px;cursor:pointer;font-weight:900;">Text Now</button>
                </form>
                <form method="post" action="/leads/{l.id}/call-now" style="margin:0">
                  <button type="submit" style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:8px 12px;border-radius:12px;cursor:pointer;font-weight:900;">Call Now</button>
                </form>
                <form method="post" action="/leads/delete/{l.id}" style="margin:0" onsubmit="return confirm('Delete lead #{l.id}?');">
                  <button type="submit" style="background:rgba(192,58,58,.18);border:1px solid rgba(192,58,58,.35);color:#e6edf3;padding:8px 12px;border-radius:12px;cursor:pointer;font-weight:900;">
                    Delete
                  </button>
                </form>
              </div>
            </div>
            """

        def sel(val: str) -> str:
            return "selected" if state == val else ""

        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;overflow-x:hidden;">
          <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;">Back</a>
          <h2 style="margin-top:12px;">All Leads</h2>

          <form method="get" action="/leads" style="display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;">
            <input name="search" value="{(search or '').replace('"','&quot;')}" placeholder="Search name/phone/email"
                   style="flex:1;min-width:240px;padding:10px;border-radius:12px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3" />
            <select name="state" style="padding:10px;border-radius:12px;border:1px solid rgba(50,74,110,.35);background:#0f1624;color:#e6edf3">
              <option value="">All workflow states</option>
              <option value="NEW" {sel("NEW")}>NEW</option>
              <option value="WORKING" {sel("WORKING")}>WORKING</option>
              <option value="CONTACTED" {sel("CONTACTED")}>CONTACTED</option>
              <option value="DO_NOT_CONTACT" {sel("DO_NOT_CONTACT")}>DO_NOT_CONTACT</option>
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

def get_lead_memory_dict(db: Session, lead_id: int) -> Dict[str, str]:
    rows = (
        db.query(LeadMemory)
        .filter(LeadMemory.lead_id == lead_id)
        .order_by(LeadMemory.key.asc())
        .all()
    )
    return {r.key: r.value for r in rows}

@app.get("/leads/{lead_id}", response_class=HTMLResponse)
def lead_detail(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Lead not found", status_code=404)

        mem = get_lead_memory_dict(db, lead.id)

        def row(label: str, key: str) -> str:
            v = mem.get(key)
            if not v:
                return ""
            return f"""
            <tr>
              <td style="padding:6px 10px;color:rgba(230,237,243,.65)">{label}</td>
              <td style="padding:6px 10px;font-weight:900;word-break:break-word">{v}</td>
            </tr>
            """

        # Show "call critical" fields first
        return HTMLResponse(f"""
        <!doctype html>
        <html>
        <head>
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>Lead #{lead.id}</title>
          <style>
            body {{ margin:0; background:#0b0f17; color:#e6edf3; font-family:system-ui; overflow-x:hidden; }}
            .wrap {{ max-width:980px; margin:20px auto; padding:18px; }}
            .card {{ background:#0f1624; border:1px solid rgba(50,74,110,.25); border-radius:16px; padding:16px; }}
            h2 {{ margin:0 0 6px 0; }}
            .muted {{ opacity:.7; font-size:13px; }}
            table {{ width:100%; border-collapse:collapse; margin-top:12px; }}
            tr {{ border-bottom:1px solid rgba(50,74,110,.2); }}
            a {{ color:#8ab4f8; text-decoration:none; }}
            .btn {{ display:inline-flex; align-items:center; justify-content:center; gap:8px; padding:10px 12px; border-radius:12px;
                    border:1px solid rgba(50,74,110,.35); background:rgba(17,24,39,.75); color:#e6edf3; cursor:pointer; font-weight:900; }}
          </style>
        </head>
        <body>
          <div class="wrap">
            <div class="card">
              <h2>#{lead.id} {lead.full_name or "Unknown"}</h2>
              <div class="muted">{lead.phone} | {lead.email or "-"}</div>
              <div class="muted">Timezone: {lead.timezone or "-"}</div>

              <div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
                <form method="post" action="/leads/{lead.id}/text-now" style="margin:0">
                  <button class="btn" type="submit">Text Now</button>
                </form>
                <form method="post" action="/leads/{lead.id}/call-now" style="margin:0">
                  <button class="btn" type="submit">Call Now</button>
                </form>
                <a class="btn" href="/leads">Back</a>
              </div>

              <h3 style="margin-top:18px;">Call Prep</h3>
              <table>
                {row("Tier", "tier")}
                {row("Product", "product_interest")}
                {row("Coverage Requested", "coverage_requested")}
                {row("US State", "us_state")}
                {row("DOB", "birthdate")}
                {row("Inquiry / Reference", "lead_reference")}
                {row("Lead Source", "lead_source")}
              </table>

              <h3 style="margin-top:18px;">Raw Notes (from PDF/OCR)</h3>
              <div style="white-space:pre-wrap; opacity:.9; font-size:13px; background:rgba(11,15,23,.65); border:1px solid rgba(50,74,110,.25); padding:12px; border-radius:14px;">
                {(mem.get("raw_text") or "")[:4000]}
              </div>

              <div style="margin-top:16px;">
                <a href="/dashboard">Back to dashboard</a>
              </div>
            </div>
          </div>
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
# Text Now / Call Now buttons
# =========================
@app.post("/leads/{lead_id}/text-now")
def text_now(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Lead not found", status_code=404)

        first = safe_first_name(lead.full_name)
        msg = (
            f"Hi{(' ' + first) if first else ''}, this is Nick's office. "
            "You requested life insurance information. "
            "Do you want a quick quote now?"
        )

        # Create action record always
        a = Action(
            lead_id=lead.id,
            type="TEXT",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({"to": lead.phone, "message": msg}),
            created_at=_now(),
        )
        db.add(a)
        db.commit()

        # Try immediate send (will fail if 10DLC not approved; error will be visible in Actions page)
        try:
            send_lead_sms(lead.phone, msg)
            a.status = "DONE"
            a.finished_at = _now()
            db.commit()
            _log(db, lead.id, None, "TEXT_NOW_SENT", msg)
            db.commit()
        except Exception as e:
            a.status = "FAILED"
            a.error = str(e)[:500]
            db.commit()
            _log(db, lead.id, None, "TEXT_NOW_FAILED", str(e)[:500])
            db.commit()

        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    finally:
        db.close()

@app.post("/leads/{lead_id}/call-now")
def call_now(lead_id: int):
    db = SessionLocal()
    try:
        lead = db.query(Lead).filter_by(id=lead_id).first()
        if not lead:
            return HTMLResponse("Lead not found", status_code=404)

        a = Action(
            lead_id=lead.id,
            type="CALL",
            status="PENDING",
            tool="twilio",
            payload_json=json.dumps({"to": lead.phone, "lead_id": lead.id}),
            created_at=_now(),
        )
        db.add(a)
        db.commit()

        try:
            if not _make_call:
                raise RuntimeError("Call function not available in twilio_client.py")
            _make_call(lead.phone, lead.id)
            a.status = "DONE"
            a.finished_at = _now()
            db.commit()
            _log(db, lead.id, None, "CALL_NOW_STARTED", f"to={lead.phone}")
            db.commit()
        except Exception as e:
            a.status = "FAILED"
            a.error = str(e)[:500]
            db.commit()
            _log(db, lead.id, None, "CALL_NOW_FAILED", str(e)[:500])
            db.commit()

        # Live call + transcript requires Twilio Media Streams + websocket.
        # This build logs and captures recordings (webhook below); transcript wiring is next step.
        return RedirectResponse(f"/leads/{lead.id}", status_code=303)
    finally:
        db.close()


# =========================
# Actions / Activity pages
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
              <div style="opacity:.75;font-size:12px">{str(a.created_at)[:19]} tool={a.tool}</div>
              <div style="opacity:.9;white-space:pre-wrap">{(a.payload_json or "")[:260]}</div>
              <div style="color:#ffb4b4;opacity:.95">{(a.error or "")[:260]}</div>
            </div>
            """
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;overflow-x:hidden;">
        <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;">Back</a>
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
              <span style="opacity:.75">lead={l.lead_id} run={l.run_id} {str(l.created_at)[:19]}</span>
              <div style="white-space:pre-wrap;opacity:.95">{(l.detail or "")[:1400]}</div>
            </div>
            """
        return HTMLResponse(f"""
        <html><body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:1100px;margin:0 auto;overflow-x:hidden;">
        <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;">Back</a>
        <h2>Activity</h2>
        <div style="background:#0f1624;padding:14px;border-radius:16px;border:1px solid rgba(50,74,110,.25)">
          {rows or "No activity"}
        </div>
        </body></html>
        """)
    finally:
        db.close()


# =========================
# Twilio inbound webhooks (SMS + recording)
# =========================
def classify_inbound_text(body: str) -> str:
    t = (body or "").strip().lower()
    if any(x in t for x in ["stop", "unsubscribe", "do not contact", "dont contact", "dnc"]):
        return "STOP"
    if any(x in t for x in ["appointment", "appt", "schedule", "book"]):
        return "APPT"
    if any(x in t for x in ["call me", "ready", "yes", "yep", "yeah", "now", "interested"]):
        return "HOT"
    if any(x in t for x in ["how much", "price", "cost", "quote", "coverage", "premium", "term", "whole", "iul", "annuity"]):
        return "QUESTION"
    return "NEUTRAL"

def cancel_pending_actions(db: Session, lead_id: int, reason: str):
    actions = db.query(Action).filter(Action.lead_id == lead_id, Action.status == "PENDING").all()
    for a in actions:
        a.status = "SKIPPED"
        a.error = f"Canceled: {reason}"
        a.finished_at = _now()
    _log(db, lead_id, None, "ACTIONS_CANCELED", reason)

@app.post("/twilio/sms/inbound")
def twilio_sms_inbound(
    From: str = Form(...),
    To: str = Form(...),
    Body: str = Form(...),
    MessageSid: str = Form("")
):
    db = SessionLocal()
    try:
        from_phone = normalize_phone(From) or (From or "").strip()
        to_phone = normalize_phone(To) or (To or "").strip()
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

        # Always forward inbound reply to you (wake for money later via escalation rules)
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

        # Store quick "learning" signals (offline)
        mem_set(db, lead.id, "last_inbound_intent", intent)
        mem_set(db, lead.id, "last_inbound_text", body[:500])

        if intent in ["HOT", "QUESTION", "APPT"]:
            lead.state = "CONTACTED"
        else:
            lead.state = "WORKING"
        lead.updated_at = _now()
        db.commit()

        # Wake rules: ALWAYS notify for HOT/APPT
        if intent in ["HOT", "APPT"]:
            notify_owner(db, lead, f"WAKE: Lead intent={intent}. Call now.", tag="WAKE_FOR_MONEY")

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


# =========================
# Offline AI Assistant API (no OpenAI required)
# =========================
@app.post("/api/assistant")
async def assistant_api(payload: dict):
    msg = (payload.get("message") or "").strip()
    db = SessionLocal()
    try:
        _log(db, None, None, "ASSISTANT_COMMAND", msg)
        db.commit()

        low = msg.lower()

        if not low:
            return {"reply": "Try: counts | run planner | execute | lead 123 | wake rules"}

        if "counts" in low:
            total = db.query(Lead).count()
            new = db.query(Lead).filter(Lead.state == "NEW").count()
            working = db.query(Lead).filter(Lead.state == "WORKING").count()
            contacted = db.query(Lead).filter(Lead.state == "CONTACTED").count()
            dnc = db.query(Lead).filter(Lead.state == "DO_NOT_CONTACT").count()
            pend = db.query(Action).filter(Action.status == "PENDING").count()
            return {"reply": f"Counts: total={total} NEW={new} WORKING={working} CONTACTED={contacted} DNC={dnc} pending={pend}"}

        if "run planner" in low or (("run" in low) and ("planner" in low)):
            out = plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
            return {"reply": json.dumps(out, indent=2)}

        if "execute" in low:
            out = execute_pending_actions(db, limit=5)
            return {"reply": json.dumps(out, indent=2)}

        if low.startswith("lead "):
            nums = re.findall(r"\d+", low)
            if not nums:
                return {"reply": "Usage: lead 123"}
            lead_id = int(nums[0])
            lead = db.query(Lead).filter_by(id=lead_id).first()
            if not lead:
                return {"reply": f"No lead with id {lead_id}"}
            mem = get_lead_memory_dict(db, lead.id)
            core = {
                "id": lead.id,
                "name": lead.full_name,
                "phone": lead.phone,
                "email": lead.email,
                "workflow": lead.state,
                "timezone": lead.timezone,
            }
            top = {k: mem.get(k) for k in ["tier", "product_interest", "coverage_requested", "us_state", "birthdate", "lead_reference"]}
            return {"reply": json.dumps({"core": core, "call_prep": top}, indent=2)}

        if "wake" in low and "rules" in low:
            return {"reply": "Wake rules: HOT/APPT replies trigger WAKE_FOR_MONEY alert to OWNER_MOBILE immediately. Quiet hours do not block owner alerts."}

        return {"reply": "Try: counts | run planner | execute | lead 123 | wake rules"}
    finally:
        db.close()

@app.post("/agenda/report")
def agenda_report(
    action_id: int = Form(...),
    outcome: str = Form(...),
    note: str = Form(""),
):
    db = SessionLocal()
    try:
        action = db.query(Action).filter(Action.id == action_id).first()
        if not action:
            return RedirectResponse("/agenda", status_code=303)

        # Mark the action as completed by human
        action.status = "DONE"

        # Save human notes so AI can reason
        if note:
            db.add(LeadMemory(
                lead_id=action.lead_id,
                key="last_human_note",
                value=note[:2000],
                updated_at=datetime.utcnow(),
            ))

        # Log outcome for AI decision-making
        db.add(AuditLog(
            lead_id=action.lead_id,
            event="HUMAN_OUTCOME",
            detail=f"outcome={outcome} note={note[:500]}",
        ))

        db.commit()
    finally:
        db.close()

    return RedirectResponse("/agenda", status_code=303)

# =========================
# GOOGLE CALENDAR AUTH
# =========================

#from fastapi.responses import RedirectResponse
#from google_auth_oauthlib.flow import Flow
#import os
#import json

#@app.get("/auth/google")
#def google_auth():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.environ["GOOGLE_CLIENT_ID"],
                "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
                "redirect_uris": [os.environ["GOOGLE_REDIRECT_URI"]],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    flow.redirect_uri = os.environ["GOOGLE_REDIRECT_URI"]

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    return RedirectResponse(auth_url)


#@app.get("/auth/google/callback")
#def google_callback(code: str):
    # TEMP TEST RESPONSE
    return {
        "ok": True,
        "received_code": bool(code)
    }

