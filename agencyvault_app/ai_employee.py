from datetime import datetime, timedelta

def run_ai_engine(db, Lead):
    now = datetime.utcnow()

    leads = db.query(Lead).filter(Lead.state != "DEAD").all()

    for lead in leads:
        if lead.state == "NEW":
            lead.ai_priority = 80
            lead.ai_next_action = "CONTACT"
            lead.ai_reason = "New lead"
            lead.state = "READY_TO_CONTACT"
            lead.ai_next_action_at = now

        elif lead.state == "READY_TO_CONTACT":
            lead.ai_next_action = "CONTACT"

        elif lead.state == "CONTACTED":
            lead.ai_next_action = "BOOK"

        elif lead.state == "NO_SHOW":
            lead.ai_next_action = "BOOK"

        lead.ai_last_action_at = now
