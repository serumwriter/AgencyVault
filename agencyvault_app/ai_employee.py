from datetime import datetime, timedelta, timezone

def _basic_priority(lead) -> int:
    """
    Simple deterministic scoring for now.
    """
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

def run_ai_engine(db, Lead, plan_only: bool = True, batch_size: int = 25):
    """
    Planner:
    - pulls NEW leads
    - assigns priority
    - creates an IMMEDIATE CALL task for phone-ready leads
    - moves leads to TRIAGED so they aren't reprocessed
    """
    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW")
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    actions = []
    now = datetime.now(timezone.utc)

    if not leads:
        return actions

    for lead in leads:
        priority = _basic_priority(lead)

        reason_parts = []
        if lead.phone:
            reason_parts.append("Has phone")
        if lead.email:
            reason_parts.append("Has email")
        reason_parts.append(f"Priority={priority}")

        # Update AI fields (safe)
        lead.ai_priority = priority
        lead.ai_next_action = "CALL" if lead.phone else "REVIEW"
        lead.ai_reason = "; ".join(reason_parts)
        lead.ai_last_action_at = now

        # If callable, make it immediate; otherwise schedule review later
        if lead.phone:
            lead.ai_next_action_at = now
        else:
            lead.ai_next_action_at = now + timedelta(hours=4)

        # Move out of NEW so we don’t loop forever
        lead.state = "TRIAGED"

        # --- TASKS ---
        # 1) Always record triage
        actions.append({
            "type": "LEAD_TRIAGED",
            "lead_id": lead.id,
            "priority": priority,
            "note": lead.ai_reason,
        })

        # 2) IMMEDIATE CALL task (this is the key change)
        if lead.phone:
            actions.append({
                "type": "CALL",
                "lead_id": lead.id,
                "priority": priority,
                "run_at": now.isoformat(),
                "note": "Initial outreach call (planned).",
            })

    db.commit()
    return actions
