# agencyvault_app/main.py
# AgencyVault - AI Employee Command Center (single-file, copy/paste)
# CHUNK 1/9 — imports, app init, core helpers, import normalization (SAFE + CLOSED)

import csv
import io
import json
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, UploadFile, File, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from sqlalchemy import text, or_
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

# IMPORTANT: keep package-local imports (Render layout)
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
    from pypdf import PdfReader  # type: ignore
    PDF_OK = True
except Exception:
    PDF_OK = False

# Optional OCR
try:
    from PIL import Image  # type: ignore
    import pytesseract  # type: ignore
    OCR_OK = True
except Exception:
    OCR_OK = False


app = FastAPI(title="AgencyVault - AI Employee")


# =========================
# Startup / Schema
# =========================
@app.on_event("startup")
def _startup():
    # Safe create; does not drop/alter tables
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
    "mortgage", "final", "expense", "annuity", "inquiry",
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

    if low in BAD_NAME_WORDS:
        return "Unknown"

    if any(x in low for x in ["bronze", "silver", "gold", "platinum", "fresh", "aged"]):
        parts = [p for p in re.split(r"[\s,]+", low) if p]
        if parts and all(p in TIER_WORDS or p in BAD_NAME_WORDS for p in parts):
            return "Unknown"

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
    """
    Upsert LeadMemory key/value.
    IMPORTANT: this must NEVER call itself (no recursion).
    """
    k = (key or "").strip()[:120]
    v = (value or "").strip()
    if not k or not v:
        return

    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=k).first()
    if row:
        row.value = v[:12000]
        row.updated_at = _now()
    else:
        db.add(LeadMemory(
            lead_id=lead_id,
            key=k,
            value=v[:12000],
            updated_at=_now(),
        ))
        

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

    start = 1 if isinstance(rows[0], list) and _looks_like_header(rows[0]) else 0

    for r in rows[start:]:
        if not isinstance(r, list):
            continue

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
    If no separators are found, returns one block.
    """
    t = (raw or "").strip()
    if not t:
        return []

    boundary = re.compile(r"(?im)^\s*(inquiry\s*id|lead\s*id)\s*[:#]")

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

    for line in (block or "").splitlines():
        s = clean_text(line)
        if not s:
            continue
        if len(s) > 45:
            continue
        low = s.lower()
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
        if not primary_phone:
            continue

        name = _guess_name_from_block(b)

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
        m = re.search(
            r"(?im)^\s*(Requested Coverage|Coverage Amount|Face Value|Current Coverage Amount)\s*:\s*([$]?\s*[\d,]+)\s*$",
            b,
        )
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
    Returns:
      {"ok": bool, "created": bool, "merged": bool, "skipped": bool, "reason": "...", "lead_id": int|None}
    Enforces: phone mandatory
    """
    phone = normalize_phone(item.get("phone") or "")
    if not phone:
        return {"ok": True, "skipped": True, "reason": "missing_phone", "lead_id": None}

    email = clean_text(item.get("email") or "")
    full_name = safe_full_name(item.get("full_name"))

    existing = db.query(Lead).filter(Lead.phone == phone).first()
    if existing:
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

    extras = dict(item)
    extras.pop("phone", None)
    extras.pop("email", None)
    extras.pop("full_name", None)

    mem_set(db, lead.id, "source_tag", source_tag)
    mem_set(db, lead.id, "source_type", source_tag)
    mem_bulk_set(db, lead.id, extras)

    return {"ok": True, "created": True, "merged": False, "skipped": False, "lead_id": lead.id}


# ===== END CHUNK 1/9 =====
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

    # NOTE: with_for_update(skip_locked=True) requires Postgres (you are on Postgres)
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

            # Due time (for scheduled calls/texts)
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
                # payload must have: to, message
                send_lead_sms(payload["to"], payload["message"])
            elif a.type == "CALL":
                if not _make_call:
                    raise RuntimeError("Call function not available in twilio_client.py")
                _make_call(payload["to"], payload.get("lead_id"))
            elif a.type == "APPOINTMENT":
                # Appointments are "planned" items; worker doesn't call calendar yet.
                a.status = "DONE"
                a.finished_at = _now()
                executed += 1
                continue
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


# Allow GET so you can click it in browser
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


# =========================
# Planner helpers (safe defaults)
# =========================
def ai_schedule_appointment(db: Session, lead_id: int, note: str = "Call") -> None:
    """
    AI-only scheduler (local only):
    - Picks next open 30-minute slot (string)
    - Creates an APPOINTMENT Action (PENDING)
    """
    appts = (
        db.query(Action)
        .filter(Action.type == "APPOINTMENT")
        .order_by(Action.created_at.asc())
        .all()
    )

    blocked = set()
    for a in appts:
        try:
            payload = json.loads(a.payload_json or "{}")
            when = payload.get("when")
            if when:
                blocked.add(str(when))
        except Exception:
            continue

    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    candidate = now + timedelta(hours=1)

    slot = None
    for _ in range(48):  # ~2 days lookahead in 30-min steps
        candidate_slot = candidate.strftime("%Y-%m-%d %H:%M")
        if candidate_slot not in blocked:
            slot = candidate_slot
            break
        candidate += timedelta(minutes=30)

    if not slot:
        return

    db.add(Action(
        lead_id=lead_id,
        type="APPOINTMENT",
        status="PENDING",
        tool="internal",
        payload_json=json.dumps({
            "when": slot,
            "note": note,
            "tz": "local",
            "name": "AI Scheduled Call",
        }),
        created_at=_now(),
    ))
    _log(db, lead_id, None, "AI_APPOINTMENT_PLANNED", f"slot={slot}")


def plan_actions(db: Session, batch_size: int = 25) -> Dict[str, Any]:
    """
    SAFE PLANNER:
    - Looks at NEW leads
    - Creates 1 TEXT action per lead (PENDING)
    - Never sends immediately
    - Never blocks web requests
    """
    planned = 0
    skipped = 0

    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW")
        .order_by(Lead.created_at.asc())
        .limit(int(batch_size))
        .all()
    )

    for lead in leads:
        # If already has pending action, skip
        already = (
            db.query(Action)
            .filter(Action.lead_id == lead.id, Action.status == "PENDING")
            .first()
        )
        if already:
            skipped += 1
            continue

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
            payload_json=json.dumps({
                "to": lead.phone,
                "message": msg,
                "reason": "New lead: first touch text",
            }),
            created_at=_now(),
        ))

        lead.state = "WORKING"
        lead.updated_at = _now()
        planned += 1

    out = {"ok": True, "planned": planned, "skipped": skipped, "batch_size": int(batch_size)}
    _log(db, None, None, "AI_PLAN", json.dumps(out)[:5000])
    return out


@app.get("/ai/plan")
def ai_plan():
    db = SessionLocal()
    try:
        out = plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
        db.commit()
        return out
    finally:
        db.close()


# ===== END CHUNK 2/9 =====
# =========================
# Agenda (single next task) + Workday start + Report outcome
# =========================
@app.get("/agenda", response_class=HTMLResponse)
def agenda():
    db = SessionLocal()
    try:
        row = (
            db.query(Action, Lead)
            .join(Lead, Lead.id == Action.lead_id)
            .filter(Action.status == "PENDING")
            .order_by(Action.created_at.asc())
            .first()
        )

        if not row:
            body = "<p>No tasks right now. Click <b>Start My Workday</b>.</p>"
        else:
            a, l = row
            payload = {}
            try:
                payload = json.loads(a.payload_json or "{}")
            except Exception:
                payload = {}

            reason = payload.get("reason", "AI decided this is next")
            due = payload.get("due_at")

            when = "Do now"
            if due:
                when = f"Scheduled for {due}"

            # Helpful display for TEXT actions
            msg = ""
            if a.type == "TEXT":
                msg = (payload.get("message") or "").strip()

            msg_html = ""
            if msg:
                msg_html = f"""
                <div style="margin-top:10px;">
                  <div style="opacity:.8;font-size:13px;margin-bottom:6px;">Suggested text</div>
                  <div style="white-space:pre-wrap;background:rgba(11,15,23,.65);border:1px solid rgba(50,74,110,.25);padding:12px;border-radius:14px;">
                    {msg[:1200]}
                  </div>
                </div>
                """

            body = f"""
            <h2 style="margin:0 0 10px 0;">Next Task</h2>

            <div style="margin-top:10px;line-height:1.5">
              <b>Lead:</b> {l.full_name or "Unknown"}<br>
              <b>Phone:</b> {l.phone}<br>
              <b>Status:</b> {l.state}
            </div>

            <div style="margin-top:10px;line-height:1.5">
              <b>Action:</b> {a.type}<br>
              <b>When:</b> {when}<br>
              <b>Why:</b> {reason}
            </div>

            {msg_html}

            <form method="post" action="/agenda/report" style="margin-top:14px">
              <input type="hidden" name="action_id" value="{a.id}" />

              <div style="opacity:.8;font-size:13px;margin-top:10px;">What happened?</div>
              <textarea name="note"
                placeholder="Paste what the lead said or what happened"
                style="width:100%;min-height:90px;margin-top:8px;background:rgba(11,15,23,.75);color:#e6edf3;border:1px solid rgba(50,74,110,.35);border-radius:14px;padding:12px;"></textarea>

              <div style="margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;">
                <button type="submit" name="outcome" value="talked"
                  style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
                  Talked / Replied
                </button>
                <button type="submit" name="outcome" value="no_answer"
                  style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
                  No Answer
                </button>
                <button type="submit" name="outcome" value="not_interested"
                  style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
                  Not Interested
                </button>
                <button type="submit" name="outcome" value="booked"
                  style="background:#111827;border:1px solid rgba(50,74,110,.35);color:#e6edf3;padding:10px 14px;border-radius:12px;cursor:pointer;font-weight:900;">
                  Booked
                </button>
              </div>

              <div style="margin-top:10px;opacity:.7;font-size:12px;">
                Tip: If they booked, include date/time + timezone in your note (e.g. "Jan 9 2pm Mountain").
              </div>
            </form>
            """

        return HTMLResponse(f"""
        <html>
        <head>
          <meta name="viewport" content="width=device-width, initial-scale=1" />
          <title>Agenda</title>
        </head>
        <body style="background:#0b0f17;color:#e6edf3;font-family:system-ui;padding:20px;max-width:980px;margin:0 auto;">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
            <div>
              <h1 style="margin:0;">AI Agenda</h1>
              <div style="opacity:.75;font-size:13px;margin-top:2px;">Do tasks top-to-bottom. Report outcomes so AI can decide next steps.</div>
            </div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;">
              <a href="/dashboard" style="color:#8ab4f8;text-decoration:none;font-weight:900;">Dashboard</a>
              <a href="/actions" style="color:#8ab4f8;text-decoration:none;font-weight:900;">Action Queue</a>
            </div>
          </div>

          <div style="background:#0f1624;border:1px solid rgba(50,74,110,.25);border-radius:16px;padding:16px;margin-top:14px;">
            {body}
          </div>
        </body>
        </html>
        """)
    finally:
        db.close()


@app.post("/workday/start")
def start_workday():
    """
    Enterprise mode:
    - Plans work safely (no blocking sends)
    - Sends user straight to /agenda
    """
    db = SessionLocal()
    try:
        plan_actions(db, batch_size=int(os.getenv("AI_BATCH_SIZE", "25")))
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/agenda", status_code=303)


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

        lead = db.query(Lead).filter(Lead.id == action.lead_id).first()

        # Mark the action as completed by human
        action.status = "DONE"
        action.finished_at = _now()

        # Save human notes so AI can reason (lead may be missing if deleted)
        if lead and note:
            mem_set(db, lead.id, "last_human_note", note[:2000])

        # Outcome log for planner
        db.add(AuditLog(
            lead_id=action.lead_id,
            run_id=None,
            event="HUMAN_OUTCOME",
            detail=f"action_id={action.id} type={action.type} outcome={outcome} note={note[:1200]}",
            created_at=_now(),
        ))

        # Minimal workflow updates (safe defaults)
        if lead:
            if outcome in ["talked", "booked"]:
                lead.state = "CONTACTED"
            elif outcome in ["not_interested"]:
                lead.state = "DO_NOT_CONTACT"
                cancel_pending_actions(db, lead.id, "Human marked not interested")
            else:
                lead.state = "WORKING"
            lead.updated_at = _now()

        db.commit()
    finally:
        db.close()

    return RedirectResponse("/agenda", status_code=303)


# ===== END CHUNK 3/9 =====
# =========================
# Dashboard helpers (SAFE)
# =========================
from sqlalchemy import or_

def _kpi_card(label: str, value: Any, sub: str = "") -> str:
    return f"""
    <div class="kpi">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value">{value}</div>
      <div class="kpi-sub">{sub}</div>
    </div>
    """

def _fmt_dt(s: str) -> str:
    try:
        return datetime.fromisoformat(s).strftime("%b %d %I:%M %p")
    except Exception:
        return (s or "")[:40]

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

def _upcoming_appts_local(db: Session, limit: int = 8) -> List[Dict[str, Any]]:
    """
    Local-only calendar (until Google sync is live).
    Convention:
      - LeadMemory key 'appt_time' = ISO string
      - Optional 'appt_note'
    """
    rows = (
        db.query(LeadMemory)
        .filter(LeadMemory.key == "appt_time")
        .order_by(LeadMemory.updated_at.desc().nullslast())
        .limit(200)
        .all()
    )

    items: List[Dict[str, Any]] = []
    for r in rows:
        lead = db.query(Lead).filter(Lead.id == r.lead_id).first()
        if not lead:
            continue
        when = (r.value or "").strip()
        if not when:
            continue
        note = mem_get(db, lead.id, "appt_note") or ""
        tz = lead.timezone or (os.getenv("DEFAULT_TIMEZONE") or "America/Denver")
        items.append({
            "lead_id": lead.id,
            "name": lead.full_name or "Unknown",
            "when": when,
            "tz": tz,
            "note": note[:180],
        })
        if len(items) >= limit:
            break
    return items


# =========================
# Dashboard (header + stats)
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

        # Activity feed (limited + safe)
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

        # Newest leads (with memory map)
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

        # Calendar (local)
        appts = _upcoming_appts_local(db, limit=8)
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
            appt_html = '<div class="muted">No appointments stored yet.</div>'

        pause_label = "Paused" if paused else "Running"

        # --- HTML START ---
        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>AgencyVault - AI Employee</title>
<!-- styles injected in chunk 5 -->
</head>
<body>
<!-- layout + content injected in chunk 5 -->
</body>
</html>
        """)
       return HTMLResponse(f""" ... """)
   
    finally:
        db.close()

def _queue_action(db: Session, lead_id: int, action_type: str, payload: Dict[str, Any], tool: str = "internal") -> Optional[int]:
    try:
        a = Action(
            lead_id=lead_id,
            type=action_type,
            status="PENDING",
            tool=tool,
            payload_json=_safe_json(payload),
            created_at=_now(),
        )
        db.add(a)
        db.flush()
        return a.id
    except Exception as e:
        _log(db, lead_id, None, "ACTION_QUEUE_FAILED", f"type={action_type} err={str(e)[:300]}")
        return None

def _default_text_for_lead(lead: Lead) -> str:
    first = safe_first_name(lead.full_name or "")
    # Keep it short + compliant
    return (
        f"Hi{(' ' + first) if first else ''}, this is Nick's office. "
        "You requested life insurance info — want a quick quote today?"
    )
    
@app.get("/ai/plan")
def ai_plan(batch_size: int = 25):
    db = SessionLocal()
    try:
        out = plan_actions(db, batch_size=batch_size)
        db.commit()
        return out
    finally:
        db.close()

# ===== END CHUNK 7/9 =====
# =========================
# Worker: execute PENDING actions (SAFE, NON-BLOCKING)
# =========================
# This chunk closes execution cleanly and fixes:
# - quiet hours handling
# - due_at parsing
# - never crashing the worker
# - never double-executing actions

def _parse_payload(a: Action) -> Dict[str, Any]:
    try:
        return json.loads(a.payload_json or "{}")
    except Exception:
        return {}

def execute_pending_actions(db: Session, limit: int = 5) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 5), 50))
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
            payload = _parse_payload(a)
            lead = db.query(Lead).filter(Lead.id == a.lead_id).first()

            if not lead:
                a.status = "FAILED"
                a.error = "Lead missing"
                failed += 1
                continue

            # Respect due_at (calls scheduled in future)
            due_at = payload.get("due_at")
            if due_at:
                try:
                    if nowv < datetime.fromisoformat(due_at):
                        skipped += 1
                        continue
                except Exception:
                    pass

            # Respect quiet hours for outreach
            tz_name = lead.timezone or infer_timezone_from_phone(lead.phone)
            if a.type in ("TEXT", "CALL"):
                if not allowed_to_contact_now(tz_name):
                    skipped += 1
                    continue

            # Execute
            if a.type == "TEXT":
                send_lead_sms(payload.get("to"), payload.get("message"))
            elif a.type == "CALL":
                if not _make_call:
                    raise RuntimeError("Call function not configured")
                _make_call(payload.get("to"), payload.get("lead_id"))
            elif a.type == "APPOINTMENT":
                # Appointments are planning artifacts only (no external call)
                pass
            else:
                raise RuntimeError(f"Unknown action type: {a.type}")

            a.status = "DONE"
            a.finished_at = _now()
            executed += 1

        except Exception as e:
            a.status = "FAILED"
            a.error = str(e)[:500]
            failed += 1

    return {
        "ok": True,
        "executed": executed,
        "failed": failed,
        "skipped": skipped,
    }

@app.get("/worker/execute")
def worker_execute(limit: int = 5):
    db = SessionLocal()
    try:
        out = execute_pending_actions(db, limit=limit)
        _log(db, None, None, "WORKER_EXECUTE", json.dumps(out))
        db.commit()
        return out
    finally:
        db.close()
