from datetime import datetime, timedelta, timezone

# ---------------------------------
# CONFIG (SAFE DEFAULTS)
# ---------------------------------
MAX_CALL_ATTEMPTS_PER_DAY = 3
CALL_RETRY_MINUTES = 15

# ---------------------------------
# PRIORITY SCORING
# ---------------------------------
def _basic_priority(lead) -> int:
    score = 0
    if lead.phone:
        score += 50
    if lead.email:
        score += 15

    if lead.created_at:
        age_hours = (datetime.utcnow() - lead.created_at).total_seconds() / 3600
        if age_hours < 24:
            score += 20
        elif age_hours < 72:
            score += 10

    return score

# ---------------------------------
# CALL WINDOW (UTC-SAFE)
# ---------------------------------
def _can_call_now(now_utc: datetime) -> bool:
    hour = now_utc.hour
    # Conservative US-safe window
    return 14 <= hour or hour < 2

def _next_call_time(now_utc: datetime) -> datetime:
    if _can_call_now(now_utc):
        return now_utc
    if now_utc.hour < 14:
        return now_utc.replace(hour=14, minute=0, second=0, microsecond=0)
    return (now_utc + timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )

# ---------------------------------
# POWER DIALER AI ENGINE
# ---------------------------------
def run_ai_engine(db, Lead, plan_only: bool = True, batch_size: int = 25):
    now = datetime.now(timezone.utc)
    actions = []

    leads = (
        db.query(Lead)
        .filter(Lead.state.in_(["NEW", "TRIAGED"]))
        .order_by(Lead.ai_priority.desc(), Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        # Ensure counters exist
        lead.call_attempts = lead.call_attempts or 0

        priority = _basic_priority(lead)
        lead.ai_priority = priority
        lead.ai_last_action_at = now

        # Reset daily attempts if day changed
        if lead.last_call_attempt_at:
            if lead.last_call_attempt_at.date() != now.date():
                lead.call_attempts = 0

        # Decide behavior
        if not lead.phone:
            lead.ai_next_action = "REVIEW"
            lead.ai_reason = "No phone number"
            lead.ai_next_action_at = now + timedelta(hours=4)
            lead.state = "TRIAGED"
            continue

        if lead.call_attempts >= MAX_CALL_ATTEMPTS_PER_DAY:
            lead.ai_next_action = "WAIT"
            lead.ai_reason = "Max daily attempts reached"
            lead.ai_next_action_at = now + timedelta(hours=12)
            lead.state = "WAITING"
            continue

        if lead.last_call_attempt_at:
            minutes_since = (now - lead.last_call_attempt_at).total_seconds() / 60
            if minutes_since < CALL_RETRY_MINUTES:
                lead.ai_next_action = "WAIT"
                lead.ai_reason = f"Cooldown ({int(CALL_RETRY_MINUTES - minutes_since)} min left)"
                lead.ai_next_action_at = lead.last_call_attempt_at + timedelta(minutes=CALL_RETRY_MINUTES)
                lead.state = "WAITING"
                continue

        # Call window check
        next_time = _next_call_time(now)
        if next_time > now:
            lead.ai_next_action = "WAIT"
            lead.ai_reason = "Outside legal call window"
            lead.ai_next_action_at = next_time
            lead.state = "WAITING"
            continue

        # PLAN CALL
        lead.call_attempts += 1
        lead.last_call_attempt_at = now
        lead.ai_next_action = "CALL"
        lead.ai_reason = f"Attempt {lead.call_attempts}"
        lead.ai_next_action_at = now
        lead.state = "CALLING"

        actions.append({
            "type": "CALL",
            "lead_id": lead.id,
            "priority": priority,
            "attempt": lead.call_attempts,
            "note": "Planned call (no execution yet)",
        })

    db.commit()
    return actions
