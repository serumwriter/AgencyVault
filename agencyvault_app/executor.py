import json
import os
import time
from datetime import datetime
from sqlalchemy.orm import Session
import pytz

from .database import SessionLocal
from .models import Action, Lead, AgentRun, AuditLog, Message
from .twilio_client import send_lead_sms, make_call_with_recording


# =========================
# Helpers
# =========================

def _now():
    return datetime.utcnow()


def _log(db: Session, run_id: int | None, lead_id: int | None, event: str, detail: str):
    db.add(AuditLog(
        run_id=run_id,
        lead_id=lead_id,
        event=event,
        detail=(detail or "")[:5000],
        created_at=_now()
    ))


def _parse_payload(payload_json: str) -> dict:
    try:
        return json.loads(payload_json or "{}")
    except Exception:
        return {}


def _due_ok(payload: dict) -> bool:
    due = payload.get("due_at")
    if not due:
        return True
    try:
        dt = datetime.fromisoformat(due.replace("Z", ""))
        return dt <= _now()
    except Exception:
        return True


# =========================
# Compliance: Timezone Rules
# =========================

def allowed_to_contact(lead: Lead) -> bool:
    """
    Enforces 8am–9pm LOCAL TIME.
    If timezone is unknown → DO NOT CONTACT.
    """
    if not lead.timezone:
        return False

    try:
        tz = pytz.timezone(lead.timezone)
    except Exception:
        return False

    local_hour = datetime.now(tz).hour
    return 8 <= local_hour < 21


# =========================
# Executor Loop
# =========================

def run_executor_loop():
    sleep_s = int((os.getenv("EXECUTOR_SLEEP_SECONDS") or "30").strip())

    while True:
        db = SessionLocal()
        run = None

        try:
            run = AgentRun(
                mode="execution",
                status="STARTED",
                batch_size=0,
                notes="Executor tick"
            )
            db.add(run)
            db.commit()
            db.refresh(run)

            _log(db, run.id, None, "EXECUTOR_TICK", "Scanning for pending actions")
            db.commit()

            actions = (
                db.query(Action)
                .filter(Action.status == "PENDING")
                .order_by(Action.created_at.asc())
                .limit(20)
                .all()
            )

            executed = 0

            for a in actions:
                try:
                    payload = _parse_payload(a.payload_json)

                    if not _due_ok(payload):
                        continue

                    lead = db.query(Lead).filter_by(id=a.lead_id).first()
                    if not lead:
                        a.status = "FAILED"
                        a.error = "Lead not found"
                        a.finished_at = _now()
                        _log(db, run.id, a.lead_id, "ACTION_FAILED", a.error)
                        db.commit()
                        continue

                    if lead.state == "DO_NOT_CONTACT":
                        a.status = "SKIPPED"
                        a.error = "Lead is DO_NOT_CONTACT"
                        a.finished_at = _now()
                        _log(db, run.id, lead.id, "ACTION_SKIPPED_DNC", a.error)
                        db.commit()
                        continue

                    # Timezone enforcement
                    if not allowed_to_contact(lead):
                        a.status = "SKIPPED"
                        a.error = "Outside allowed local time window"
                        a.finished_at = _now()
                        _log(db, run.id, lead.id, "ACTION_SKIPPED_TIME", a.error)
                        db.commit()
                        continue

                    a.status = "RUNNING"
                    a.started_at = _now()
                    db.commit()

                    first_name = (lead.full_name or "there").split(" ")[0].strip()

                    # =========================
                    # TEXT MESSAGE
                    # =========================
                    if a.type == "TEXT":

                        if lead.last_contacted_at:
                            # Aged lead
                            msg = (
                                f"Hi {first_name}, this is Nick's office. "
                                "You had requested life insurance info before. "
                                "Do you still want help with a quick quote?"
                            )
                        else:
                            # Fresh lead
                            msg = (
                                f"Hi {first_name}, this is Nick's office. "
                                "You recently requested life insurance information. "
                                "I can get you a quick quote — want me to send it?"
                            )

                        sid = send_lead_sms(lead.phone, msg)

                        db.add(Message(
                            lead_id=lead.id,
                            direction="OUT",
                            channel="SMS",
                            from_number=os.getenv("TWILIO_FROM_NUMBER", ""),
                            to_number=lead.phone,
                            body=msg,
                            provider_sid=sid or "",
                            created_at=_now()
                        ))

                        _log(db, run.id, lead.id, "TEXT_SENT", msg)

                    # =========================
                    # PHONE CALL
                    # =========================
                    elif a.type == "CALL":

                        call_script = (
                            f"Hi {first_name}. This is Nick calling about the life "
                            "insurance information you requested. I just need to ask "
                            "you a couple quick questions so I can get you accurate pricing. "
                            "If now is not a good time, you can hang up or text me back. "
                            "Otherwise, stay on the line."
                        )

                        sid = make_call_with_recording(
                            to=lead.phone,
                            lead_id=lead.id,
                            script=call_script
                        )

                        _log(db, run.id, lead.id, "CALL_STARTED", f"sid={sid}")

                    else:
                        a.status = "SKIPPED"
                        a.error = f"Unknown action type: {a.type}"
                        a.finished_at = _now()
                        _log(db, run.id, lead.id, "ACTION_SKIPPED_UNKNOWN", a.error)
                        db.commit()
                        continue

                    # Success bookkeeping
                    lead.last_contacted_at = _now()
                    lead.updated_at = _now()

                    a.status = "SUCCEEDED"
                    a.finished_at = _now()
                    a.error = ""

                    executed += 1
                    db.commit()

                except Exception as e:
                    a.status = "FAILED"
                    a.error = str(e)[:2000]
                    a.finished_at = _now()
                    _log(db, run.id, a.lead_id, "ACTION_ERROR", a.error)
                    db.commit()

            run.status = "SUCCEEDED"
            run.finished_at = _now()
            _log(db, run.id, None, "EXECUTOR_DONE", f"executed={executed}")
            db.commit()

        except Exception as e:
            if run:
                run.status = "FAILED"
                run.finished_at = _now()
                _log(db, run.id, None, "EXECUTOR_CRASH", str(e)[:2000])
                db.commit()

        finally:
            db.close()

        time.sleep(sleep_s)
