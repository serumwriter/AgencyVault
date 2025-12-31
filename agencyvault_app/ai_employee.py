from datetime import datetime, timedelta
import re

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

# ----------------------------
# NAME RESOLUTION (HUMAN-LIKE)
# ----------------------------
def resolve_name(lead):
    # 1. Trust real-looking names
    if lead.full_name:
        name = lead.full_name.strip()
        if (
            len(name.split()) >= 2
            and not any(bad in name.lower() for bad in ["lead", "life", "center", "insurance"])
        ):
            return name

    # 2. Infer from email
    if lead.email and "@" in lead.email:
        local = lead.email.split("@")[0]
        local = re.sub(r"[0-9_\.]+", " ", local)
        parts = [p.capitalize() for p in local.split() if len(p) > 2]
        if len(parts) >= 2:
            return " ".join(parts[:2])

    # 3. Leave as-is if uncertain
    return lead.full_name or "Unknown"

# ----------------------------
# DEDUPE
# ----------------------------
def find_duplicate(db, Lead, lead):
    phone = norm_phone(lead.phone)
    if not phone:
        return None

    return (
        db.query(Lead)
        .filter(Lead.phone == phone, Lead.id != lead.id)
        .first()
    )

# ----------------------------
# PRODUCT DETECTION
# ----------------------------
def detect_product(lead):
    text = (lead.ai_reason or "").lower()

    if any(k in text for k in ["annuity", "401k", "ira", "cd", "rollover"]):
        return "ANNUITY"

    if any(k in text for k in ["iul", "indexed", "cash value", "tax free"]):
        return "IUL"

    return "LIFE"

# ----------------------------
# PRIORITY SCORING
# ----------------------------
def score_priority(lead, product):
    score = 10

    if product == "ANNUITY":
        score += 50
    elif product == "IUL":
        score += 35
    else:
        score += 20

    if lead.ai_reason and any(
        k in lead.ai_reason.lower()
        for k in ["now", "today", "asap", "immediately"]
    ):
        score += 30

    return min(score, 100)

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
        if not lead.phone:
            lead.state = "SKIPPED"
            continue

        # Cooldown protection
        if lead.ai_last_action_at:
            if lead.ai_last_action_at + timedelta(minutes=COOLDOWN_MINUTES) > now:
                continue

        # Deduplication
        if find_duplicate(db, Lead, lead):
            lead.state = "DUPLICATE"
            continue

        # Resolve name like a human would
        lead.full_name = resolve_name(lead)

        product = detect_product(lead)
        score = score_priority(lead, product)

        if score >= 80:
            action = "ESCALATE_NOW"
        elif score >= 40:
            action = "CALL"
        else:
            action = "IGNORE"

        lead.ai_priority = score
        lead.ai_next_action = action
        lead.ai_reason = f"{product} lead"
        lead.ai_last_action_at = now
        lead.state = "READY"

        if action in ("CALL", "ESCALATE_NOW"):
            actions.append({
                "type": action,
                "lead_id": lead.id
            })

    db.commit()
    return actions
