from datetime import datetime, timedelta

MAX_ATTEMPTS = 3
COOLDOWN_MINUTES = 15

def run_ai_engine(db, Lead, batch_size=25):
    now = datetime.utcnow()
    actions = []

    leads = (
        db.query(Lead)
        .filter(Lead.state == "NEW")
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        if not lead.phone:
            lead.state = "SKIPPED"
            continue

        lead.ai_priority = 100
        lead.ai_next_action = "CALL"
        lead.ai_reason = "New lead with phone"
        lead.ai_last_action_at = now
        lead.state = "READY"

        actions.append({
            "type": "CALL",
            "lead_id": lead.id
        })

    db.commit()
    return actions
