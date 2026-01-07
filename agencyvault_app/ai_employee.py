import json
import re
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from .models import Lead, Action, AgentRun, AuditLog, LeadMemory

BAD_NAME_WORDS = {
    "lead", "bronze", "silver", "gold", "ethos",
    "facebook", "insurance", "prospect", "unknown", "test",
    "client", "customer", "policy", "quote", "applicant"
}

# -------------------------
# Helpers
# -------------------------

def _now():
    return datetime.utcnow()

def _log(db: Session, run_id: int | None, lead_id: int | None, event: str, detail: str):
    db.add(AuditLog(
        run_id=run_id,
        lead_id=lead_id,
        event=event,
        detail=(detail or "")[:5000],
    ))

def mem_get(db: Session, lead_id: int, key: str) -> str | None:
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def mem_set(db: Session, lead_id: int, key: str, value: str):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = _now()
    else:
        db.add(LeadMemory(
            lead_id=lead_id,
            key=key,
            value=value,
            updated_at=_now(),
        ))

def safe_first_name(full_name: str) -> str:
    if not full_name:
        return ""
    first = full_name.strip().split()[0].lower()
    first = re.sub(r"[^a-zA-Z\-']", "", first).strip()
    if not first or first in BAD_NAME_WORDS or len(first) < 2:
        return ""
    return first.capitalize()

# -------------------------
# Messaging builders
# -------------------------

def build_sms_1(lead: Lead) -> str:
    first = safe_first_name(lead.full_name or "")
    greet = f"Hi {first}," if first else "Hi,"
    return (
        f"{greet} this is Nick’s office. I’m following up on the life insurance info you requested. "
        "What’s the best time for a quick call today?"
    )

def build_sms_nudge(lead: Lead) -> str:
    first = safe_first_name(lead.full_name or "")
    greet = f"Hi {first}," if first else "Hi,"
    return (
        f"{greet} just checking back — I can help you get a plan in place fast. "
        "Want me to call you, or do you prefer texting?"
    )

# -------------------------
# AI Scheduler (AUTONOMOUS)
# -------------------------

def ai_schedule_appointment(db: Session, lead_id: int, reason: str):
    """
    AI-only scheduler.
    Books the next available 30-minute slot.
    """

    # Existing appointments
    appts = (
        db.query(Action)
        .filter(Action.type == "APPOINTMENT")
        .all()
    )

    blocked = set()
    for a in appts:
        try:
            payload = json.loads(a.payload_json or "{}")
            when = payload.get("when")
            if when:
                blocked.add(when)
        except Exception:
            pass

    now = _now().replace(minute=0, second=0, microsecond=0)
    candidate = now + timedelta(hours=1)

    for _ in range(48):  # ~24 hours ahead
        slot = candidate.strftime("%Y-%m-%d %H:%M")
        if slot not in blocked:
            break
        candidate += timedelta(minutes=30)

    db.add(Action(
        lead_id=lead_id,
        type="APPOINTMENT",
        status="PENDING",
        tool="calendar",
        payload_json=json.dumps({
            "when": slot,
            "reason": reason,
        }),
    ))

# -------------------------
# AI Planner
# -------------------------

def plan_actions(db: Session, batch_size: int = 25) -> dict:
    now = _now()

    run = AgentRun(
        mode="planning",
        status="STARTED",
        batch_size=batch_size,
        notes="AI employee planner",
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    _log(db, run.id, None, "AI_PLANNER_START", f"batch_size={batch_size}")

    leads = (
        db.query(Lead)
        .filter(
            Lead.phone.isnot(None),
            Lead.state.in_(["NEW", "WORKING"]),
        )
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    planned = 0
    considered = 0

    for lead in leads:
        considered += 1

        if lead.state == "DO_NOT_CONTACT":
            continue

        if mem_get(db, lead.id, "quarantined") == "1":
            _log(db, run.id, lead.id, "LEAD_SKIPPED_QUARANTINE", "Lead is quarantined")
            continue

        if not (lead.phone or "").startswith("+"):
            mem_set(db, lead.id, "quarantined", "1")
            _log(db, run.id, lead.id, "LEAD_QUARANTINED", f"Bad phone format: {lead.phone}")

            db.add(Action(
                lead_id=lead.id,
                type="REVIEW",
                status="PENDING",
                tool="",
                payload_json=json.dumps({"reason": "Bad phone format"}),
            ))
            planned += 1
            continue

        # -------------------------
        # NEW LEAD → SPEED TO LEAD
        # -------------------------
        if lead.state == "NEW":
            sms1 = build_sms_1(lead)

            db.add(Action(
                lead_id=lead.id,
                type="TEXT",
                status="PENDING",
                tool="twilio",
                payload_json=json.dumps({
                    "due_at": now.isoformat(),
                    "message": sms1,
                    "reason": "speed_to_lead",
                }),
            ))
            planned += 1
            _log(db, run.id, lead.id, "AI_PLANNED_TEXT", "Speed-to-lead SMS")

            db.add(Action(
                lead_id=lead.id,
                type="CALL",
                status="PENDING",
                tool="twilio",
                payload_json=json.dumps({
                    "due_at": (now + timedelta(minutes=2)).isoformat(),
                    "reason": "speed_to_lead_call",
                }),
            ))
            planned += 1
            _log(db, run.id, lead.id, "AI_PLANNED_CALL", "Speed-to-lead CALL")

            # 🔥 AI schedules the call automatically
            ai_schedule_appointment(db, lead.id, "speed_to_lead_call")

            lead.state = "WORKING"
            lead.updated_at = now

        # -------------------------
        # WORKING → FOLLOW-UP
        # -------------------------
        if lead.state == "WORKING":
            last = lead.last_contacted_at
            if not last or (now - last) > timedelta(hours=20):
                sms2 = build_sms_nudge(lead)

                db.add(Action(
                    lead_id=lead.id,
                    type="TEXT",
                    status="PENDING",
                    tool="twilio",
                    payload_json=json.dumps({
                        "due_at": (now + timedelta(minutes=5)).isoformat(),
                        "message": sms2,
                        "reason": "followup_nudge",
                    }),
                ))
                planned += 1
                _log(db, run.id, lead.id, "AI_PLANNED_TEXT", "Follow-up nudge")

                # 🔥 AI schedules follow-up call
                ai_schedule_appointment(db, lead.id, "followup_call")

    run.status = "SUCCEEDED"
    run.finished_at = _now()
    _log(db, run.id, None, "AI_PLANNER_DONE", f"planned={planned} considered={considered}")

    db.commit()
    return {
        "run_id": run.id,
        "planned_actions": planned,
        "considered": considered,
    }
