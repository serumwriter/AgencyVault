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

def _now():
    return datetime.utcnow()

def _log(db: Session, run_id: int | None, lead_id: int | None, event: str, detail: str):
    db.add(AuditLog(run_id=run_id, lead_id=lead_id, event=event, detail=(detail or "")[:5000]))

def mem_get(db: Session, lead_id: int, key: str) -> str | None:
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    return row.value if row else None

def mem_set(db: Session, lead_id: int, key: str, value: str):
    row = db.query(LeadMemory).filter_by(lead_id=lead_id, key=key).first()
    if row:
        row.value = value
        row.updated_at = _now()
    else:
        db.add(LeadMemory(lead_id=lead_id, key=key, value=value, updated_at=_now()))

def safe_first_name(full_name: str) -> str:
    if not full_name:
        return ""
    first = full_name.strip().split()[0].lower()
    first = re.sub(r"[^a-zA-Z\-']", "", first).strip()
    if not first:
        return ""
    if first in BAD_NAME_WORDS or len(first) < 2:
        return ""
    return first.capitalize()

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

def plan_actions(db: Session, batch_size: int = 25) -> dict:
    now = _now()

    run = AgentRun(mode="planning", status="STARTED", batch_size=batch_size, notes="AI employee planner")
    db.add(run)
    db.commit()
    db.refresh(run)

    _log(db, run.id, None, "AI_PLANNER_START", f"batch_size={batch_size}")

    # Only safe states
    leads = (
        db.query(Lead)
        .filter(Lead.phone.isnot(None), Lead.state.in_(["NEW", "WORKING"]))
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    planned = 0
    considered = 0

    for lead in leads:
        considered += 1

        # Hard stop compliance
        if lead.state == "DO_NOT_CONTACT":
            continue

        # Quarantine check
        if mem_get(db, lead.id, "quarantined") == "1":
            _log(db, run.id, lead.id, "LEAD_SKIPPED_QUARANTINE", "Lead is quarantined")
            continue

        # Guardrails: never plan if phone looks broken
        if not (lead.phone or "").startswith("+"):
            mem_set(db, lead.id, "quarantined", "1")
            _log(db, run.id, lead.id, "LEAD_QUARANTINED", f"Bad phone format: {lead.phone}")
            # Create a review action for human
            db.add(Action(
                lead_id=lead.id,
                type="REVIEW",
                status="PENDING",
                tool="",
                payload_json=json.dumps({"reason": "Bad phone format - needs review"}),
            ))
            planned += 1
            continue

        # If NEW: immediate speed-to-lead
        if lead.state == "NEW":
            sms1 = build_sms_1(lead)

            db.add(Action(
                lead_id=lead.id,
                type="TEXT",
                status="PENDING",
                tool="twilio",
                payload_json=json.dumps({"due_at": now.isoformat(), "message": sms1, "reason": "speed_to_lead"}),
            ))
            planned += 1
            _log(db, run.id, lead.id, "AI_PLANNED_TEXT", "Speed-to-lead SMS planned")

            db.add(Action(
                lead_id=lead.id,
                type="CALL",
                status="PENDING",
                tool="twilio",
                payload_json=json.dumps({"due_at": (now + timedelta(minutes=2)).isoformat(), "reason": "speed_to_lead_call"}),
            ))
            planned += 1
            _log(db, run.id, lead.id, "AI_PLANNED_CALL", "Speed-to-lead CALL planned")

            lead.state = "WORKING"
            lead.updated_at = now

        # If WORKING: ensure a follow-up exists (smart nudge)
        if lead.state == "WORKING":
            # Avoid spamming: plan only if last contacted is old enough
            last = lead.last_contacted_at
            if not last or (now - last) > timedelta(hours=20):
                sms2 = build_sms_nudge(lead)
                db.add(Action(
                    lead_id=lead.id,
                    type="TEXT",
                    status="PENDING",
                    tool="twilio",
                    payload_json=json.dumps({"due_at": (now + timedelta(minutes=5)).isoformat(), "message": sms2, "reason": "followup_nudge"}),
                ))
                planned += 1
                _log(db, run.id, lead.id, "AI_PLANNED_TEXT", "Follow-up nudge planned")

    run.status = "SUCCEEDED"
    run.finished_at = _now()
    _log(db, run.id, None, "AI_PLANNER_DONE", f"planned={planned} considered={considered}")

    db.commit()
    return {"run_id": run.id, "planned_actions": planned, "considered": considered}
