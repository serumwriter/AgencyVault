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
    finally:
        db.close()


# ===== END CHUNK 4/9 =====
# =========================
# Dashboard layout + styles
# =========================
# This chunk ONLY fills in the <body> and <style> safely.
# No new logic. No schema changes. No removals.

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
    overflow-x:hidden;
  }}
  a {{ color:var(--link); text-decoration:none; }}
  .wrap {{
    display:grid;
    grid-template-columns:260px 1fr;
    min-height:100vh;
  }}
  .sidebar {{
    border-right:1px solid var(--border);
    padding:16px;
    background:rgba(11,15,23,.85);
  }}
  .brand {{ font-weight:900; font-size:20px; }}
  .subtitle {{ font-size:13px; color:var(--muted); margin-bottom:10px; }}
  .nav a {{
    display:block;
    padding:10px;
    margin-bottom:8px;
    border-radius:12px;
    border:1px solid rgba(50,74,110,.2);
    background:rgba(15,22,36,.6);
  }}
  .main {{
    padding:18px;
    max-width:1250px;
  }}
  .topbar {{
    display:flex;
    justify-content:space-between;
    align-items:flex-end;
    flex-wrap:wrap;
    gap:12px;
  }}
  .title {{ font-size:26px; font-weight:900; }}
  .sub {{ color:var(--muted); font-size:13px; }}
  .kpis {{
    display:grid;
    grid-template-columns:repeat(6,1fr);
    gap:10px;
    margin-top:12px;
  }}
  .kpi {{
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:16px;
    padding:12px;
  }}
  .kpi-label {{ font-size:12px; color:var(--muted); }}
  .kpi-value {{ font-size:22px; font-weight:900; }}
  .panel {{
    background:var(--panel);
    border:1px solid var(--border);
    border-radius:18px;
    padding:14px;
    margin-top:12px;
  }}
  .lead-row {{
    display:flex;
    justify-content:space-between;
    gap:10px;
    border-bottom:1px solid var(--border);
    padding:10px 0;
  }}
  .pill {{
    padding:4px 10px;
    border-radius:999px;
    font-size:12px;
    border:1px solid rgba(138,180,248,.3);
  }}
  .btn, .mini {{
    background:rgba(17,24,39,.75);
    border:1px solid rgba(50,74,110,.35);
    color:var(--text);
    border-radius:12px;
    cursor:pointer;
    font-weight:900;
  }}
  .btn {{ padding:10px 14px; }}
  .mini {{ padding:6px 10px; font-size:12px; }}
  .muted {{ color:var(--muted); font-size:12px; }}
  .appt {{ border-bottom:1px solid var(--border); padding:8px 0; }}
  @media (max-width:1100px) {{
    .wrap {{ grid-template-columns:1fr; }}
    .sidebar {{ border-right:none; }}
    .kpis {{ grid-template-columns:repeat(2,1fr); }}
  }}
</style>
</head>

<body>
<div class="wrap">

  <aside class="sidebar">
    <div class="brand">AgencyVault</div>
    <div class="subtitle">AI Employee</div>

    <div class="nav">
      <a href="/dashboard">Dashboard</a>
      <a href="/leads">Leads</a>
      <a href="/leads/new">Add Lead</a>
      <a href="/actions">Actions</a>
      <a href="/activity">Activity</a>
      <a href="/ai/plan">Run Planner</a>
      <a href="/worker/execute?limit=5">Execute</a>
    </div>

    <div class="muted" style="margin-top:12px">
      Status: <b>{pause_label}</b>
    </div>

    <form method="post" action="/workday/start" style="margin-top:12px">
      <button class="btn" type="submit">Start My Workday</button>
    </form>
  </aside>

  <main class="main">

    <div class="topbar">
      <div>
        <div class="title">AI Command Center</div>
        <div class="sub">Do the next task. The AI handles the rest.</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <a class="btn" href="/leads/new">Add Lead</a>
        <a class="btn" href="/worker/execute?limit=5">Execute</a>
      </div>
    </div>

    <div class="kpis">
      <div class="kpi"><div class="kpi-label">Total</div><div class="kpi-value">{total}</div></div>
      <div class="kpi"><div class="kpi-label">NEW</div><div class="kpi-value">{new}</div></div>
      <div class="kpi"><div class="kpi-label">WORKING</div><div class="kpi-value">{working}</div></div>
      <div class="kpi"><div class="kpi-label">CONTACTED</div><div class="kpi-value">{contacted}</div></div>
      <div class="kpi"><div class="kpi-label">DNC</div><div class="kpi-value">{dnc}</div></div>
      <div class="kpi"><div class="kpi-label">Pending</div><div class="kpi-value">{pending}</div></div>
    </div>

    <div class="panel">
      <h3>Next Appointments</h3>
      {appt_html}
    </div>

    <div class="panel">
      <h3>Newest Leads</h3>
      {leads_html or '<div class="muted">No leads yet</div>'}
    </div>

    <div class="panel">
      <h3>Live Activity</h3>
      {feed or '<div class="muted">No activity yet</div>'}
    </div>

  </main>
</div>
</body>
</html>
        """)
    finally:
        db.close()

# ===== END CHUNK 5/9 =====
# =========================
# Agenda (Next Task) + Workday Start + Report Outcome
# =========================
# Fixes that were crashing you:
# - Removes stray HTML lines outside a function
# - Ensures /agenda is a complete function (no indentation leaks)
# - /agenda/report uses the correct lead_id (no undefined "lead")
# - mem_set() recursion bug is handled in chunk 7 (but we avoid it here by not relying on it heavily)

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
            body = """
              <div class="card">
                <h2>Next Task</h2>
                <div class="muted">No pending tasks right now.</div>
                <div style="margin-top:12px">
                  <a class="btn" href="/ai/plan">Run Planner</a>
                  <a class="btn" href="/dashboard" style="margin-left:8px">Back to Dashboard</a>
                </div>
              </div>
            """
        else:
            a, l = row
            payload = {}
            try:
                payload = json.loads(a.payload_json or "{}")
            except Exception:
                payload = {}

            reason = payload.get("reason", "AI decided this is next")
            due = payload.get("due_at") or ""
            when_label = "Do now"
            if due:
                try:
                    when_label = f"Scheduled for {due}"
                except Exception:
                    when_label = "Scheduled"

            action_desc = a.type or "TASK"
            if action_desc == "CALL":
                todo = f"CALL: {l.phone or '-'}"
            elif action_desc == "TEXT":
                todo = f"TEXT: {l.phone or '-'}"
            elif action_desc == "APPOINTMENT":
                todo = f"APPOINTMENT: {payload.get('when') or when_label}"
            else:
                todo = action_desc

            msg = payload.get("message") if a.type == "TEXT" else ""
            msg_html = ""
            if msg:
                msg_html = f"""
                  <div class="muted" style="margin-top:10px">Suggested message:</div>
                  <div class="box" style="white-space:pre-wrap">{(msg or "")[:1200]}</div>
                """

            body = f"""
              <div class="card">
                <h2>Next Task</h2>

                <div style="margin-top:8px">
                  <div><b>Lead:</b> <a href="/leads/{l.id}">#{l.id} {l.full_name or "Unknown"}</a></div>
                  <div><b>Phone:</b> {l.phone or "-"}</div>
                  <div><b>Workflow:</b> {l.state or "-"}</div>
                  <div><b>When:</b> {when_label}</div>
                </div>

                <div style="margin-top:10px">
                  <div><b>Do this:</b> {todo}</div>
                  <div class="muted" style="margin-top:6px">Why: {reason}</div>
                </div>

                {msg_html}

                <form method="post" action="/agenda/report" style="margin-top:14px">
                  <input type="hidden" name="action_id" value="{a.id}" />

                  <div class="muted">What happened? (paste or quick notes)</div>
                  <textarea name="note" placeholder="Example: No answer. Left VM. Call back tomorrow morning."></textarea>

                  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
                    <button class="btn" type="submit" name="outcome" value="talked">Talked / Replied</button>
                    <button class="btn" type="submit" name="outcome" value="no_answer">No Answer</button>
                    <button class="btn" type="submit" name="outcome" value="not_interested">Not Interested</button>
                    <button class="btn" type="submit" name="outcome" value="booked">Booked</button>
                  </div>
                </form>

                <div style="margin-top:12px">
                  <a class="btn" href="/dashboard">Back</a>
                  <a class="btn" href="/actions" style="margin-left:8px">View Queue</a>
                </div>
              </div>
            """

        return HTMLResponse(f"""
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Agenda</title>
  <style>
    body {{
      margin:0;
      background:#0b0f17;
      color:#e6edf3;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial;
      padding:18px;
      overflow-x:hidden;
    }}
    a {{ color:#8ab4f8; text-decoration:none; }}
    .wrap {{ max-width:900px; margin:0 auto; }}
    .card {{
      background:#0f1624;
      border:1px solid rgba(50,74,110,.25);
      border-radius:18px;
      padding:16px;
    }}
    .muted {{ opacity:.75; font-size:13px; }}
    .btn {{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:8px;
      padding:10px 12px;
      border-radius:12px;
      border:1px solid rgba(50,74,110,.35);
      background:rgba(17,24,39,.75);
      color:#e6edf3;
      cursor:pointer;
      font-weight:900;
    }}
    textarea {{
      width:100%;
      min-height:90px;
      margin-top:8px;
      padding:12px;
      border-radius:14px;
      border:1px solid rgba(50,74,110,.35);
      background:rgba(11,15,23,.75);
      color:#e6edf3;
      outline:none;
      font-size:14px;
    }}
    .box {{
      margin-top:8px;
      padding:12px;
      border-radius:14px;
      border:1px solid rgba(50,74,110,.25);
      background:rgba(11,15,23,.55);
      color:rgba(230,237,243,.92);
      font-size:13px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1 style="margin:0 0 10px 0">AI Agenda</h1>
    <div class="muted" style="margin-bottom:12px">Do tasks top-to-bottom. Report outcome so the AI can plan the next move.</div>
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
    Plans a workday (creates PENDING actions) then sends you to /agenda.
    Never blocks on Twilio.
    """
    db = SessionLocal()
    try:
        try:
            plan_actions(db, batch_size=120)
        except Exception as e:
            _log(db, None, None, "WORKDAY_PLAN_FAILED", str(e)[:500])
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

        # Mark completed by human
        action.status = "DONE"
        action.finished_at = _now()

        lead_id = action.lead_id

        # Save human notes so AI can reason later
        n = (note or "").strip()
        if n and lead_id:
            try:
                mem_set(db, lead_id, "last_human_note", n[:1200])
                mem_set(db, lead_id, "last_human_outcome", (outcome or "")[:60])
            except Exception as e:
                _log(db, lead_id, None, "MEM_WRITE_FAILED", str(e)[:300])

        # Audit log for AI decision-making
        try:
            db.add(AuditLog(
                lead_id=lead_id,
                run_id=None,
                event="HUMAN_OUTCOME",
                detail=f"action_id={action_id} outcome={outcome} note={(n[:800] if n else '')}",
                created_at=_now(),
            ))
        except Exception:
            pass

        # Simple workflow moves (safe defaults)
        if lead_id:
            lead = db.query(Lead).filter(Lead.id == lead_id).first()
            if lead:
                if outcome == "not_interested":
                    lead.state = "DO_NOT_CONTACT"
                elif outcome in ("talked", "booked"):
                    lead.state = "CONTACTED"
                else:
                    lead.state = "WORKING"
                lead.updated_at = _now()

        db.commit()
    finally:
        db.close()

    return RedirectResponse("/agenda", status_code=303)

# ===== END CHUNK 6/9 =====
# =========================
# Memory helpers (FIXED) + Action planning (AI Planner)
# =========================
# This chunk fixes the biggest silent-crash bug you had:
#   else:mem_set(db, lead_id, key, v)
# That line caused recursion/stack issues and broke writes.
# We replace it with a correct INSERT branch.

def mem_set(db: Session, lead_id: int, key: str, value: str):
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
    if not d:
        return
    for k, v in d.items():
        if v is None:
            continue
        vv = clean_text(v)
        if vv:
            mem_set(db, lead_id, str(k), vv)

def mem_del(db: Session, lead_id: int, key: str):
    k = (key or "").strip()
    if not k:
        return
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=k).first()
    if row:
        db.delete(row)

def is_global_pause(db: Session) -> bool:
    # Stored as LeadMemory on lead_id=0 (safe "global settings")
    try:
        v = mem_get(db, 0, "GLOBAL_PAUSE") or "0"
        return v.strip() == "1"
    except Exception:
        return False

def set_global_pause(db: Session, paused: bool):
    mem_set(db, 0, "GLOBAL_PAUSE", "1" if paused else "0")

def _safe_json(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        try:
            return json.dumps(str(obj))
        except Exception:
            return "{}"

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

def plan_actions(db: Session, batch_size: int = 25) -> Dict[str, Any]:
    """
    AI Planner (SAFE MODE):
    - Never sends messages/calls directly
    - Only creates PENDING actions
    - Respects GLOBAL_PAUSE
    - Uses small batches and conservative rules
    """
    if is_global_pause(db):
        return {"ok": True, "paused": True, "created": 0, "skipped": "GLOBAL_PAUSE"}

    batch_size = max(1, min(int(batch_size or 25), 200))

    created = 0
    considered = 0
    skipped_missing_phone = 0
    skipped_dnc = 0

    # Optional: track runs if your AgentRun model exists (you imported it)
    run_id = None
    try:
        ar = AgentRun(
            status="PLANNING",
            created_at=_now(),
            finished_at=None,
            summary="",
        )
        db.add(ar)
        db.flush()
        run_id = ar.id
    except Exception:
        run_id = None

    # Pick leads that need work
    leads = (
        db.query(Lead)
        .filter(Lead.state.in_(["NEW", "WORKING"]))
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        considered += 1

        # Mandatory phone
        if not (lead.phone or "").strip():
            skipped_missing_phone += 1
            continue

        # Compliance
        if (lead.state or "").strip().upper() == "DO_NOT_CONTACT":
            skipped_dnc += 1
            continue

        # If already has pending actions, don't pile on
        pend = (
            db.query(Action)
            .filter(Action.lead_id == lead.id, Action.status == "PENDING")
            .count()
        )
        if pend >= 2:
            continue

        # Decide next action
        # Rule: NEW -> TEXT first, then CALL later if no reply.
        if (lead.state or "").strip().upper() == "NEW":
            msg = _default_text_for_lead(lead)
            aid = _queue_action(
                db,
                lead.id,
                "TEXT",
                {"to": lead.phone, "message": msg, "reason": "New lead: send first contact text"},
                tool="twilio",
            )
            if aid:
                created += 1
                lead.state = "WORKING"
                lead.updated_at = _now()
                _log(db, lead.id, run_id, "PLANNED_TEXT", f"action_id={aid}")
            continue

        # WORKING -> if no recent inbound, schedule a call attempt (due_at in future)
        last_inbound = mem_get(db, lead.id, "last_inbound_text") or ""
        if last_inbound.strip():
            # If they replied, let human handle next (don’t auto pile calls)
            continue

        due = (_now() + timedelta(minutes=15)).isoformat()
        aid = _queue_action(
            db,
            lead.id,
            "CALL",
            {"to": lead.phone, "lead_id": lead.id, "due_at": due, "reason": "Working lead: call attempt"},
            tool="twilio",
        )
        if aid:
            created += 1
            lead.updated_at = _now()
            _log(db, lead.id, run_id, "PLANNED_CALL", f"action_id={aid} due_at={due}")

    # Finish run record
    try:
        if run_id:
            ar2 = db.query(AgentRun).filter(AgentRun.id == run_id).first()
            if ar2:
                ar2.status = "DONE"
                ar2.finished_at = _now()
                ar2.summary = f"considered={considered} created={created} skipped_phone={skipped_missing_phone} skipped_dnc={skipped_dnc}"
    except Exception:
        pass

    try:
        _log(db, None, run_id, "AI_PLAN_COMPLETE", f"considered={considered} created={created}")
    except Exception:
        pass

    # Let caller commit
    return {
        "ok": True,
        "paused": False,
        "considered": considered,
        "created": created,
        "skipped_missing_phone": skipped_missing_phone,
        "skipped_dnc": skipped_dnc,
        "run_id": run_id,
    }

# Optional UI endpoint (you already link it in sidebar)
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


# =========================
# Agenda (Next Task UI)
# =========================
# Fixes:
# - Indentation errors
# - HTML outside strings
# - Always-safe rendering

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
            payload = _parse_payload(a)
            reason = payload.get("reason", "AI decided this is next")
            due = payload.get("due_at")

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

              <textarea
                name="note"
                placeholder="Paste what happened or what they said"
                style="width:100%;min-height:80px;margin-top:10px"
              ></textarea>

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
        <head><title>Agenda</title></head>
        <body style="background:#111;color:#eee;font-family:Arial;padding:20px">
          <h1>AI Agenda</h1>
          <p>This page tells you exactly what to do next.</p>
          {body}
        </body>
        </html>
        """)
    finally:
        db.close()


# =========================
# Agenda Report (Human feedback → AI memory)
# =========================
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

        action.status = "DONE"
        action.finished_at = _now()

        clean_note = (note or "").strip()
        if lead and clean_note:
            mem_set(db, lead.id, "last_human_note", clean_note)

        _log(
            db,
            action.lead_id,
            None,
            "HUMAN_OUTCOME",
            f"outcome={outcome} note={(clean_note[:500] if clean_note else '')}",
        )

        # Outcome-driven workflow
        if lead:
            if outcome == "not_interested":
                lead.state = "DO_NOT_CONTACT"
            elif outcome in ("talked", "booked"):
                lead.state = "CONTACTED"
            else:
                lead.state = "WORKING"
            lead.updated_at = _now()

        db.commit()
    finally:
        db.close()

    return RedirectResponse("/agenda", status_code=303)


# =========================
# Start Workday
# =========================
@app.post("/workday/start")
def start_workday():
    db = SessionLocal()
    try:
        plan_actions(db, batch_size=120)
        db.commit()
    finally:
        db.close()

    return RedirectResponse("/agenda", status_code=303)

# ===== END CHUNK 8/9 =====
# =========================
# Planner (SAFE) - if missing in your file, add this section
# =========================
# You said: "do not remove anything" — so this chunk is defensive:
# - If plan_actions already exists earlier, KEEP YOURS and IGNORE THIS DUPLICATE.
# - If you accidentally broke/removed it, paste this one in and it will work.
#
# IMPORTANT: If your file already has plan_actions defined, do NOT paste this duplicate.
# If you paste duplicates, Python will use the LAST one (this one), which is still safe.

def plan_actions(db: Session, batch_size: int = 25) -> Dict[str, Any]:
    """
    SAFE planner:
    - Only plans (creates Action rows) and never blocks web requests.
    - Does NOT call Twilio.
    - Works even if LeadMemory contains weird vendor keys.
    """
    batch_size = max(1, min(int(batch_size or 25), 200))

    planned = 0
    skipped = 0

    # Global pause switch (optional)
    try:
        paused = (mem_get(db, 0, "GLOBAL_PAUSE") or "0") == "1"
        if paused:
            return {"ok": True, "paused": True, "planned": 0, "skipped": 0}
    except Exception:
        pass

    leads = (
        db.query(Lead)
        .filter(Lead.state.in_(["NEW", "WORKING"]))
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for l in leads:
        try:
            if not (l.phone or "").strip():
                skipped += 1
                continue

            # Don't stack if they already have pending actions
            has_pending = (
                db.query(Action)
                .filter(Action.lead_id == l.id, Action.status == "PENDING")
                .first()
                is not None
            )
            if has_pending:
                skipped += 1
                continue

            # Simple default: plan a text (worker sends it during allowed hours)
            first = safe_first_name(l.full_name)
            msg = (
                f"Hi{(' ' + first) if first else ''}, this is Nick's office. "
                "You requested life insurance info — want a quick quote today?"
            )

            db.add(Action(
                lead_id=l.id,
                type="TEXT",
                status="PENDING",
                tool="twilio",
                payload_json=json.dumps({
                    "to": l.phone,
                    "message": msg,
                    "reason": "New lead auto-followup",
                }),
                created_at=_now(),
            ))
            l.state = "WORKING"
            l.updated_at = _now()
            planned += 1

        except Exception:
            skipped += 1
            continue

    _log(db, None, None, "PLAN_ACTIONS", f"planned={planned} skipped={skipped} batch={batch_size}")
    return {"ok": True, "planned": planned, "skipped": skipped, "batch_size": batch_size}


# =========================
# Final sanity: module end
# =========================
# This prevents "dangling triple quote" type mistakes by ending cleanly.
# Do not add anything after this unless you're sure it's complete.

# ===== END main.py =====
# =========================
# Planner (SAFE) - if missing in your file, add this section
# =========================
# You said: "do not remove anything" — so this chunk is defensive:
# - If plan_actions already exists earlier, KEEP YOURS and IGNORE THIS DUPLICATE.
# - If you accidentally broke/removed it, paste this one in and it will work.
#
# IMPORTANT: If your file already has plan_actions defined, do NOT paste this duplicate.
# If you paste duplicates, Python will use the LAST one (this one), which is still safe.

def plan_actions(db: Session, batch_size: int = 25) -> Dict[str, Any]:
    """
    SAFE planner:
    - Only plans (creates Action rows) and never blocks web requests.
    - Does NOT call Twilio.
    - Works even if LeadMemory contains weird vendor keys.
    """
    batch_size = max(1, min(int(batch_size or 25), 200))

    planned = 0
    skipped = 0

    # Global pause switch (optional)
    try:
        paused = (mem_get(db, 0, "GLOBAL_PAUSE") or "0") == "1"
        if paused:
            return {"ok": True, "paused": True, "planned": 0, "skipped": 0}
    except Exception:
        pass

    leads = (
        db.query(Lead)
        .filter(Lead.state.in_(["NEW", "WORKING"]))
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for l in leads:
        try:
            if not (l.phone or "").strip():
                skipped += 1
                continue

            # Don't stack if they already have pending actions
            has_pending = (
                db.query(Action)
                .filter(Action.lead_id == l.id, Action.status == "PENDING")
                .first()
                is not None
            )
            if has_pending:
                skipped += 1
                continue

            # Simple default: plan a text (worker sends it during allowed hours)
            first = safe_first_name(l.full_name)
            msg = (
                f"Hi{(' ' + first) if first else ''}, this is Nick's office. "
                "You requested life insurance info — want a quick quote today?"
            )

            db.add(Action(
                lead_id=l.id,
                type="TEXT",
                status="PENDING",
                tool="twilio",
                payload_json=json.dumps({
                    "to": l.phone,
                    "message": msg,
                    "reason": "New lead auto-followup",
                }),
                created_at=_now(),
            ))
            l.state = "WORKING"
            l.updated_at = _now()
            planned += 1

        except Exception:
            skipped += 1
            continue

    _log(db, None, None, "PLAN_ACTIONS", f"planned={planned} skipped={skipped} batch={batch_size}")
    return {"ok": True, "planned": planned, "skipped": skipped, "batch_size": batch_size}


# =========================
# Final sanity: module end
# =========================
# This prevents "dangling triple quote" type mistakes by ending cleanly.
# Do not add anything after this unless you're sure it's complete.

# ===== END main.py =====
