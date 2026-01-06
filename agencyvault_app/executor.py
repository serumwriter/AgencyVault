import json
import os
import time
from datetime import datetime
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import Action, Lead, AgentRun, AuditLog, Message
from .twilio_client import send_lead_sms, make_call_with_recording

from datetime import datetime
import pytz

def allowed_to_contact(lead) -> bool:
    if not lead.timezone:
        return False

    tz = pytz.timezone(lead.timezone)
    local_hour = datetime.now(tz).hour

    return 8 <= local_hour < 21
def _now():
    return datetime.utcnow()

def _log(db: Session, run_id: int | None, lead_id: int | None, event: str, detail: str):
    db.add(AuditLog(run_id=run_id, lead_id=lead_id, event=event, detail=(detail or "")[:5000]))

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

def run_executor_loop():
    sleep_s = int((os.getenv("EXECUTOR_SLEEP_SECONDS") or "30").strip() or "30")

    while True:
        db = SessionLocal()
        run = None
        try:
            run = AgentRun(mode="execution", status="STARTED", batch_size=0, notes="Executor tick")
            db.add(run)
            db.commit()
            db.refresh(run)

            _log(db, run.id, None, "EXECUTOR_TICK", "Scanning for due actions")

            # Keep batches small and safe
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
                        a.error = "Missing lead"
                        a.finished_at = _now()
                        _log(db, run.id, a.lead_id, "ACTION_FAILED", "Missing lead record")
                        db.commit()
                        continue

                    if lead.state == "DO_NOT_CONTACT":
                        a.status = "SKIPPED"
                        a.finished_at = _now()
                        _log(db, run.id, lead.id, "ACTION_SKIPPED_DNC", f"Skipped {a.type} due to DO_NOT_CONTACT")
                        db.commit()
                        continue

                    a.status = "RUNNING"
                    a.started_at = _now()
                    db.commit()

                    if a.type == "TEXT":
                        msg = (payload.get("message") or "").strip()
                        if not msg:
                            msg = "Hi, this is Nick’s office. Following up on your life insurance request."
                        sid = send_lead_sms(lead.phone, msg)

                        db.add(Message(
                            lead_id=lead.id,
                            direction="OUT",
                            channel="SMS",
                            from_number=os.getenv("TWILIO_FROM_NUMBER", ""),
                            to_number=lead.phone,
                            body=msg,
                            provider_sid=sid or "",
                        ))

                        _log(db, run.id, lead.id, "TEXT_SENT", msg)

                    elif a.type == "CALL":
                        sid = make_call_with_recording(lead.phone, lead.id)
                        _log(db, run.id, lead.id, "CALL_STARTED", f"sid={sid} to={lead.phone}")

                    elif a.type == "REVIEW":
                        _log(db, run.id, lead.id, "REVIEW_PENDING", payload.get("reason", "Review needed"))
                        # REVIEW actions remain pending until you mark done later (we keep it simple)
                        a.status = "SUCCEEDED"

                    else:
                        a.status = "SKIPPED"
                        _log(db, run.id, lead.id, "ACTION_SKIPPED", f"Unknown action type {a.type}")

                    lead.last_contacted_at = _now()
                    lead.updated_at = _now()

                    if a.status == "RUNNING":
                        a.status = "SUCCEEDED"
                    a.finished_at = _now()
                    a.error = ""

                    executed += 1
                    db.commit()

                except Exception as e:
                    # Self-heal: fail only this action, keep loop alive
                    try:
                        a.status = "FAILED"
                        a.error = str(e)[:2000]
                        a.finished_at = _now()
                        _log(db, run.id, a.lead_id, "ACTION_ERROR", a.error)
                        db.commit()
                    except Exception:
                        pass

            run.status = "SUCCEEDED"
            run.finished_at = _now()
            _log(db, run.id, None, "EXECUTOR_DONE", f"executed={executed}")
            db.commit()

        except Exception as e:
            try:
                if run:
                    run.status = "FAILED"
                    run.finished_at = _now()
                    _log(db, run.id, None, "EXECUTOR_CRASH", str(e)[:2000])
                    db.commit()
            except Exception:
                pass
        finally:
            db.close()

        time.sleep(sleep_s)
