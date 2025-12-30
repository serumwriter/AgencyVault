from datetime import datetime, timedelta

def _basic_priority(lead) -> int:
    """
    Simple deterministic scoring for now.
    Later we’ll replace/augment with true LLM scoring + timezone/call window logic.
    """
    score = 0
    if lead.phone:
        score += 50
    if lead.email:
        score += 15
    # Prefer newer leads slightly
    if lead.created_at:
        age_hours = (datetime.utcnow() - lead.created_at).total_seconds() / 3600
        if age_hours < 24:
            score += 20
        elif age_hours < 72:
            score += 10
    return score

def run_ai_engine(db, Lead, plan_only: bool = True, batch_size: int = 25):
    """
    SAFE planner:
    - pulls only NEW leads
    - processes in small batches
    - writes AI fields + state updates (safe, internal)
    - returns planned actions for /ai/run to write into ai_tasks
    """
    # Pull only NEW leads, oldest first
    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW")
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    actions = []
    now = datetime.utcnow()

    if not leads:
        return actions

    for lead in leads:
        priority = _basic_priority(lead)

        # For now: create a “review then call” plan (no calling executed)
        next_action = "REVIEW_AND_CALL"
        reason_parts = []
        if lead.phone:
            reason_parts.append("Has phone")
        if lead.email:
            reason_parts.append("Has email")
        reason_parts.append(f"Priority={priority}")

        # Update lead with AI fields (safe)
        lead.ai_priority = priority
        lead.ai_next_action = next_action
        lead.ai_reason = "; ".join(reason_parts)
        lead.ai_last_action_at = now

        # Set a conservative “next action time” placeholder
        # (Later: real 50-state calling hours logic)
        lead.ai_next_action_at = now + timedelta(minutes=5)

        # Move lead out of NEW so we don’t re-process endlessly
        lead.state = "TRIAGED"

        # Planner outputs tasks (these are NOT executed; just queued for review)
        actions.append({
            "type": "LEAD_TRIAGED",
            "lead_id": lead.id,
            "priority": priority,
            "next_action": next_action,
            "note": lead.ai_reason,
        })

        actions.append({
            "type": "SUGGEST_CALL",
            "lead_id": lead.id,
            "priority": priority,
            "suggested_after_utc": (lead.ai_next_action_at.isoformat() if lead.ai_next_action_at else None),
            "note": "Planned only. No calling executed.",
        })

    # Commit lead updates (safe internal state)
    db.commit()
    return actions
