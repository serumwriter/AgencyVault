from datetime import datetime, timedelta, timezone

# Very simple phone → timezone mapping (US only for now)
AREA_CODE_TIMEZONE = {
    # Pacific
    "209": "US/Pacific", "213": "US/Pacific", "310": "US/Pacific", "415": "US/Pacific",
    # Mountain
    "303": "US/Mountain", "406": "US/Mountain",
    # Central
    "312": "US/Central", "214": "US/Central", "713": "US/Central",
    # Eastern
    "212": "US/Eastern", "305": "US/Eastern", "404": "US/Eastern",
}

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

def _infer_timezone_from_phone(phone: str):
    if not phone or len(phone) < 5:
        return pytz.UTC
    area = phone.replace("+1", "")[:3]
    tz_name = AREA_CODE_TIMEZONE.get(area)
    return pytz.timezone(tz_name) if tz_name else pytz.UTC

def _next_allowed_call_time(local_now):
    start = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
    end = local_now.replace(hour=21, minute=0, second=0, microsecond=0)

    if local_now < start:
        return start
    if local_now > end:
        return start + timedelta(days=1)
    return local_now

def _can_call_now(local_now):
    return 8 <= local_now.hour < 21

def run_ai_engine(db, Lead, plan_only: bool = True, batch_size: int = 25):
    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW")
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    actions = []
    now_utc = datetime.now(timezone.utc)

    for lead in leads:
        priority = _basic_priority(lead)
        tz = _infer_timezone_from_phone(lead.phone)
        local_now = now_utc.astimezone(tz)

        lead.ai_priority = priority
        lead.ai_reason = f"Priority={priority}"
        lead.ai_last_action_at = now_utc

        # Always record triage
        actions.append({
            "type": "LEAD_TRIAGED",
            "lead_id": lead.id,
            "priority": priority,
        })

        if lead.phone:
            if _can_call_now(local_now):
                # CALL NOW
                lead.ai_next_action = "CALL"
                lead.ai_next_action_at = now_utc

                actions.append({
                    "type": "CALL",
                    "lead_id": lead.id,
                    "priority": priority,
                    "run_at": now_utc.isoformat(),
                    "note": "Legal call window open",
                })
            else:
                # WAIT until legal window
                next_local = _next_allowed_call_time(local_now)
                next_utc = next_local.astimezone(pytz.UTC)

                lead.ai_next_action = "WAIT"
                lead.ai_next_action_at = next_utc

                actions.append({
                    "type": "WAIT",
                    "lead_id": lead.id,
                    "priority": priority,
                    "run_at": next_utc.isoformat(),
                    "note": "Outside legal calling hours",
                })
        else:
            lead.ai_next_action = "REVIEW"
            lead.ai_next_action_at = now_utc + timedelta(hours=4)

        lead.state = "TRIAGED"

    db.commit()
    return actions
