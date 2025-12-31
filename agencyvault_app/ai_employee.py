from datetime import datetime, timedelta
import re

COOLDOWN_MINUTES = 15


# ============================================================
# Helpers
# ============================================================

def clean_text(*parts):
    return " ".join([p for p in parts if p]).lower()


def norm_phone(phone):
    if not phone:
        return None
    d = re.sub(r"\D", "", phone)
    if len(d) == 10:
        return "+1" + d
    if len(d) == 11 and d.startswith("1"):
        return "+" + d
    return None


def resolve_name(lead):
    if lead.full_name:
        name = lead.full_name.strip()
        if (
            len(name.split()) >= 2
            and not any(bad in name.lower() for bad in ["life", "insurance", "lead", "center"])
        ):
            return name

    if lead.email and "@" in lead.email:
        local = lead.email.split("@")[0]
        local = re.sub(r"[0-9_\.]+", " ", local)
        parts = [p.capitalize() for p in local.split() if len(p) > 2]
        if len(parts) >= 2:
            return " ".join(parts[:2])

    return lead.full_name or "Unknown"


def detect_product(lead):
    t = clean_text(
        getattr(lead, "source", ""),
        getattr(lead, "notes", ""),
        getattr(lead, "ai_summary", ""),
    )

    if any(k in t for k in ["annuity", "ira", "401k", "rollover", "cd"]):
        return "ANNUITY", "retirement/rollover language"

    if any(k in t for k in ["iul", "indexed", "cash value", "tax free"]):
        return "IUL", "indexed/cash value language"

    return "LIFE", "default life lane"


def detect_urgency(lead):
    t = clean_text(
        getattr(lead, "notes", ""),
        getattr(lead, "ai_summary", ""),
        getattr(lead, "source", ""),
    )
    if any(k in t for k in ["now", "today", "asap", "immediately", "right now", "ready"]):
        return True, "urgent intent language"
    return False, ""


def missing_prequal_fields(lead):
    missing = []
    if not getattr(lead, "state", None):
        missing.append("state")
    if not getattr(lead, "dob", None):
        missing.append("dob")
    if not getattr(lead, "smoker", None):
        missing.append("smoker")
    if not getattr(lead, "height", None):
        missing.append("height")
    if not getattr(lead, "weight", None):
        missing.append("weight")
    return missing


def dedupe(db, Lead, lead):
    if lead.phone:
        dup = (
            db.query(Lead)
            .filter(Lead.phone == lead.phone, Lead.id != lead.id)
            .first()
        )
        if dup:
            return True, "duplicate phone"

    if lead.email:
        dup = (
            db.query(Lead)
            .filter(Lead.email == lead.email, Lead.id != lead.id)
            .first()
        )
        if dup:
            return True, "duplicate email"

    return False, ""


# ============================================================
# MAIN AI ENGINE
# ============================================================

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
        # Hard stop: no phone
        if not lead.phone:
            lead.status = "SKIPPED_NO_PHONE"
            lead.ai_summary = "Skipped: no phone"
            continue

        # Cooldown protection
        if (
            lead.last_contacted_at
            and lead.last_contacted_at + timedelta(minutes=COOLDOWN_MINUTES) > now
        ):
            continue

        lead.full_name = resolve_name(lead)

        # Dedupe check
        is_dup, dup_ev = dedupe(db, Lead, lead)
        if is_dup:
            lead.status = "DUPLICATE"
            lead.product_interest = lead.product_interest or "UNKNOWN"
            lead.ai_confidence = 95
            lead.ai_evidence = dup_ev
            lead.needs_human = 0
            lead.ai_summary = "Duplicate detected"
            continue

        product, prod_ev = detect_product(lead)
        urgent, urg_ev = detect_urgency(lead)
        missing = missing_prequal_fields(lead)

        # Base decision
        action = "CALL"
        needs_human = 0
        confidence = 55
        evidence = [prod_ev]

        # Product-based escalation
        if product == "ANNUITY":
            action = "ESCALATE_HIGH_VALUE"
            needs_human = 1
            confidence = 85
            evidence.append("annuity lane is high value")

        elif product == "IUL":
            action = "ESCALATE_STRATEGY"
            needs_human = 1
            confidence = 75
            evidence.append("IUL strategy complexity")

        # Urgency override
        if urgent:
            action = "ESCALATE_NOW"
            needs_human = 1
            confidence = max(confidence, 90)
            evidence.append(urg_ev)

        # Missing info handling
        if missing and not urgent:
            action = "NEEDS_INFO"
            needs_human = 0
            confidence = 65
            evidence.append(f"missing prequal: {', '.join(missing)}")

        # Persist AI state
        lead.product_interest = product
        lead.ai_confidence = confidence
        lead.ai_evidence = "; ".join(evidence)
        lead.needs_human = needs_human
        lead.ai_summary = f"{product} | conf {confidence} | {lead.ai_evidence}"
        lead.last_contacted_at = now
        lead.status = "AI_PROCESSED"

        # CALL task
        actions.append({
            "type": action,
            "lead_id": lead.id,
            "confidence": confidence,
            "evidence": lead.ai_evidence,
            "needs_human": needs_human,
        })

        # Pre-call TEXT (only if calling)
        if action == "CALL":
            actions.append({
                "type": "TEXT",
                "lead_id": lead.id,
                "due_at": now + timedelta(minutes=5),
                "payload": {
                    "message": (
                        f"Hi {lead.full_name.split()[0]}, "
                        "this is a quick heads-up — I’ll be calling you shortly "
                        "about your life insurance options."
                    )
                },
            })

    db.commit()
    return actions
