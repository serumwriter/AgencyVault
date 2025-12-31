from datetime import datetime, timedelta
import re

MAX_ATTEMPTS = 3
COOLDOWN_MINUTES = 15

# ----------------------------
# NORMALIZATION
# ----------------------------
def norm_phone(phone):
    if not phone:
        return None
    d = re.sub(r"\D", "", phone)
    if len(d) == 10:
        return "+1" + d
    return d

def norm_name(name):
    if not name:
        return None
    return re.sub(r"[^a-z]", "", name.lower())

# ----------------------------
# DEDUPE
# ----------------------------
def find_duplicate(db, Lead, lead):
    phone = norm_phone(lead.phone)
    name = norm_name(lead.full_name)

    if phone:
        dup = db.query(Lead).filter(
            Lead.phone == phone,
            Lead.id != lead.id
        ).first()
        if dup:
            return dup

    if name:
        dup = db.query(Lead).filter(
            Lead.full_name.ilike(f"%{lead.full_name.split()[0]}%"),
            Lead.id != lead.id
        ).first()
        if dup:
            return dup

    return None

# ----------------------------
# PRODUCT DETECTION
# ----------------------------
def detect_product(lead):
    text = (lead.ai_reason or "").lower()

    # Explicit intent
    if any(k in text for k in ["annuity", "401k", "ira", "cd", "rollover"]):
        return "ANNUITY"

    if any(k in text for k in ["iul", "indexed", "cash value", "tax free"]):
        return "IUL"

    # Age-based fallback
    if hasattr(lead, "date_of_birth") and lead.date_of_birth:
        age = (datetime.utcnow().date() - lead.date_of_birth).days // 365
        if age >= 60:
            return "FINAL_EXPENSE"
        if age < 55:
            return "TERM"

    return "UNKNOWN"

# ----------------------------
# PRIORITY SCORING
# ----------------------------
def score_priority(lead, product):
    score = 0
    text = (lead.ai_reason or "").lower()

    # Urgency signals
    if any(k in text for k in ["now", "today", "asap", "immediately"]):
        score += 40

    if any(k in text for k in ["declined", "denied", "turned down"]):
        score += 25

    # Product value
    if product == "ANNUITY":
        score += 40
    elif product == "IUL":
        score += 30
    elif product == "FINAL_EXPENSE":
        score += 25
    elif product == "TERM":
        score += 15

    return min(score, 100)

# ----------------------------
# ACTION DECISION
# ----------------------------
def decide_action(score):
    if score >= 80:
        return "ESCALATE_NOW"
    if score >= 40:
        return "CALL"
    return "IGNORE"

# ----------------------------
# MAIN AI ENGINE
# ----------------------------
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
        # Skip unusable leads
        if not lead.phone:
            lead.state = "SKIPPED"
            continue

        # Cooldown protection
        if lead.ai_last_action_at:
            if lead.ai_last_action_at + timedelta(minutes=COOLDOWN_MINUTES) > now:
                continue

        # DEDUPE
        dup = find_duplicate(db, Lead, lead)
        if dup:
            lead.state = "DUPLICATE"
            continue

        # PRODUCT + SCORE
        product = detect_product(lead)
        score = score_priority(lead, product)
        action = decide_action(score)

        # UPDATE LEAD
        lead.ai_priority = score
        lead.ai_next_action = action
        lead.ai_reason = f"{product} lead scored {score}"
        lead.ai_last_action_at = now
        lead.state = "READY"

        if action != "IGNORE":
            actions.append({
                "type": action,
                "lead_id": lead.id
            })

    db.commit()
    return actions
