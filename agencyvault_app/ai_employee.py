from datetime import datetime, timedelta

def run_ai_engine(db, Lead, batch_size=50):
    """
    SIMPLE, RELIABLE PLANNER
    Always plans tasks so you SEE activity.
    """

    now = datetime.utcnow()
    actions = []

    leads = (
        db.query(Lead)
        .filter(Lead.phone.isnot(None))
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        # Call immediately
        actions.append({
            "type": "CALL",
            "lead_id": lead.id,
            "notes": f"Initial call to {lead.full_name}",
            "due_at": now
        })

        # Text follow-up
        actions.append({
            "type": "TEXT",
            "lead_id": lead.id,
            "notes": f"Hi {lead.full_name.split()[0]}, this is Nick’s office. Just reaching out about your life insurance request.",
            "due_at": now + timedelta(minutes=5)
        })

        # Mark lead processed so we don’t loop forever
        lead.state = "AI_PROCESSED"
        lead.last_contacted_at = now

    db.commit()
    return actions
