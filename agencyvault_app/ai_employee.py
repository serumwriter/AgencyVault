from datetime import datetime, timedelta
import re
from collections import defaultdict

# =========================
# CONFIG
# =========================
MAX_CALL_ATTEMPTS = 6
COOLDOWN_MINUTES = 15
DUPLICATE_PHONE_WEIGHT = 40
DUPLICATE_EMAIL_WEIGHT = 30
KEYWORD_WEIGHT = 15

# =========================
# NORMALIZATION
# =========================
def norm_phone(phone):
    if not phone:
        return None
    d = re.sub(r"\D", "", phone)
    if len(d) == 10:
        return "+1" + d
    return d

def clean_text(*parts):
    return " ".join(p for p in parts if p).lower()

# =========================
# NAME RESOLUTION (HUMAN-LIKE)
# =========================
def resolve_name(lead):
    if lead.full_name:
        name = lead.full_name.strip()
        if len(name.split()) >= 2 and not any(
            bad in name.lower()
            for bad in ["life", "insurance", "lead", "center"]
        ):
            return name

    if lead.email and "@" in lead.email:
        local = lead.email.split("@")[0]
        local = re.sub(r"[0-9_\.]+", " ", local)
        parts = [p.capitalize() for p in local.split() if len(p) > 2]
        if len(parts) >= 2:
            return " ".join(parts[:2])

    return lead.full_name or "Unknown"

# =========================
# PRODUCT BRAIN (MAXED)
# =========================
def detect_product_and_value(lead):
    text = clean_text(lead.source, lead.notes, lead.ai_summary)

    evidence = []
    score = 0
    product = "LIFE"

    if any(k in text for k in ["annuity", "ira", "401k", "rollover", "cd"]):
        product = "ANNUITY"
        score += 40
        evidence.append("retirement / rollover language")

    if any(k in text for k in ["iul", "indexed", "cash value", "tax free"]):
        product = "IUL"
        score += 30
        evidence.append("cash value / indexed language")

    if any(k in text for k in ["term", "mortgage", "kids", "income"]):
        evidence.append("income / family protection language")

    if any(k in text for k in ["now", "today", "asap", "ready"]):
        score += 30
        evidence.append("urgent intent")

    return product, score, "; ".join(evidence) or "default life insurance assumptions"

# =========================
# DUPLICATE DETECTION
# =========================
def detect_duplicate(db, Lead, lead):
    score = 0
    evidence = []

    if lead.phone:
        dup = db.query(Lead).filter(
            Lead.phone == lead.phone,
            Lead.id != lead.id
        ).first()
        if dup:
            score += DUPLICATE_PHONE_WEIGHT
            evidence.append("duplicate phone")

    if lead.email:
        dup = db.query(Lead).filter(
            Lead.email == lead.email,
            Lead.id != lead.id
        ).first()
        if dup:
            score += DUPLICATE_EMAIL_WEIGHT
            evidence.append("duplicate email")

    return score, "; ".join(evidence)

# =========================
# MAIN AI ENGINE
# =========================
def run_ai_engine(db, Lead, batch_size=25):
    now = datetime.utcnow()
    actions = []

    leads = (
        db.query(Lead)
        .filter(Lead.status == "New")
        .order_by(Lead.created_at.asc())
        .limit(batch_size)
        .all()
    )

    for lead in leads:
        # -------------------------
        # BASIC SANITY
        # -------------------------
        if not lead.phone:
            lead.status = "SKIPPED_NO_PHONE"
            lead.ai_summary = "Skipped: no phone"
            continue

        if lead.last_contacted_at:
            if lead.last_contacted_at + timedelta(minutes=COOLDOWN_MINUTES) > now:
                continue

        lead.full_name = resolve_name(lead)

        # -------------------------
        # DUPLICATE CHECK
        # -------------------------
        dup_score, dup_evidence = detect_duplicate(db, Lead, lead)
        if dup_score >= 40:
            lead.status = "DUPLICATE"
            lead.ai_confidence = 90
            lead.ai_evidence = dup_evidence
            lead.needs_human = 0
            lead.ai_summary = "Duplicate lead detected"
            continue

        # -------------------------
        # PRODUCT + VALUE BRAIN
        # -------------------------
        product, value_score, product_evidence = detect_product_and_value(lead)

        confidence = min(100, value_score + 30)
        needs_human = 0
        action = "CALL"

        # -------------------------
        # ESCALATION RULES
        # -------------------------
        if product == "ANNUITY":
            needs_human = 1
            action = "ESCALATE_HIGH_VALUE"

        elif product == "IUL":
            needs_human = 1
            action = "ESCALATE_STRATEGY"

        if "urgent" in product_evidence:
            needs_human = 1
            action = "ESCALATE_NOW"
            confidence = max(confidence, 90)

        if confidence < 50:
            action = "NEEDS_INFO"

        # -------------------------
        # WRITE MEMORY
        # -------------------------
        lead.product_interest = product
        lead.ai_confidence = confidence
        lead.ai_evidence = product_evidence
        lead.needs_human = needs_human
        lead.ai_summary = (
            f"{product} | confidence {confidence} | {product_evidence}"
        )
        lead.last_contacted_at = now
        lead.status = "AI_PROCESSED"

        actions.append({
            "type": action,
            "lead_id": lead.id,
            "confidence": confidence,
            "evidence": product_evidence,
            "needs_human": needs_human,
        })

    db.commit()
    return actions
