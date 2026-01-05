from datetime import datetime, timedelta
import re

BAD_NAME_WORDS = {
    "lead", "bronze", "silver", "gold", "ethos",
    "facebook", "insurance", "prospect", "unknown", "test"
}

def safe_first_name(full_name: str) -> str:
    """
    Extract a human-safe first name or return empty string.
    """
    if not full_name:
        return ""

    first = full_name.strip().split()[0].lower()

    if (
        first in BAD_NAME_WORDS
        or len(first) < 2
        or any(c.isdigit() for c in first)
    ):
        return ""

    return first.capitalize()


def run_ai_engine(db, Lead, batch_size=50):
    """
    SMART, SAFE PLANNER
    Plans calls + texts you can SEE and TRUST.
    """

    now = datetime.utcnow()
    actions = []

    leads = (
        db.query(Lead)
        .filter(
            Lead.phone.isnot(None),
            Lead.state == "NEW"
        )
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        first_name = safe_first_name(lead.full_name)

        # ---------- CALL 1 (now) ----------
        actions.append({
            "type": "CALL",
            "lead_id": lead.id,
            "notes": "Initial outbound call",
            "due_at": now
        })

        # ---------- TEXT 1 (5 min later) ----------
        text_msg = (
            f"Hi{f' {first_name}' if first_name else ''}, "
            "this is Nick’s office. I’m following up on the life insurance "
            "info you requested. I’ll give you a quick call shortly."
        )

        actions.append({
            "type": "TEXT",
            "lead_id": lead.id,
            "notes": text_msg,
            "due_at": now + timedelta(minutes=5)
        })

        # ---------- CALL 2 (next day) ----------
        actions.append({
            "type": "CALL",
            "lead_id": lead.id,
            "notes": "Second follow-up call",
            "due_at": now + timedelta(days=1)
        })

        # ---------- TEXT 2 (after missed call) ----------
        actions.append({
            "type": "TEXT",
            "lead_id": lead.id,
            "notes": (
                f"Hi{f' {first_name}' if first_name else ''}, "
                "just wanted to make sure you saw my message. "
                "Happy to help whenever it’s convenient."
            ),
            "due_at": now + timedelta(days=1, minutes=10)
        })

        # ---------- MARK LEAD ----------
        lead.state = "AI_PROCESSED"
        lead.last_contacted_at = now

    db.commit()
    return actions
