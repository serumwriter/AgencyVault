from datetime import datetime, timedelta

MAX_ATTEMPTS = 5
COOLDOWN_HOURS = 24

def decide_next_action(lead):
    """
    Decide what Sarah should do next with a lead.
    This function is READ-ONLY and makes no side effects.
    """

    # Hard stop
    if lead.status == "DNC":
        return {
            "decision": "DO_NOT_CALL",
            "reason": "Lead marked as DNC",
            "new_status": None,
            "cooldown_until": None,
            "append_note": None,
        }

    # Too many attempts
    if lead.dial_score is not None and lead.dial_score >= MAX_ATTEMPTS:
        return {
            "decision": "CLOSE_OUT",
            "reason": "Max attempts reached",
            "new_status": "CLOSED",
            "cooldown_until": None,
            "append_note": "Closed by AI: max attempts reached.",
        }

    # Cooldown window
    if lead.dialed_at:
        cooldown_until = lead.dialed_at + timedelta(hours=COOLDOWN_HOURS)
        if datetime.utcnow() < cooldown_until:
            return {
                "decision": "WAIT",
                "reason": "Cooldown window active",
                "new_status": None,
                "cooldown_until": cooldown_until,
                "append_note": None,
            }

    # Default
    return {
        "decision": "READY",
        "reason": "Eligible for contact",
        "new_status": "READY",
        "cooldown_until": None,
        "append_note": "AI marked lead as ready for next action.",
    }
