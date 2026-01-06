import os
from twilio.rest import Client
from sqlalchemy import text

from .database import engine

def _base_url() -> str:
    public = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    base = (os.getenv("BASE_URL") or "").strip()
    chosen = public or base
    if chosen.startswith("http://localhost") or chosen.startswith("https://localhost"):
        chosen = base or chosen
    return chosen.rstrip("/")

def get_twilio_client() -> Client:
    sid = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    token = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not sid or not token:
        raise RuntimeError("Twilio credentials missing (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).")
    return Client(sid, token)

def get_from_number() -> str:
    n = (os.getenv("TWILIO_FROM_NUMBER") or "").strip()
    if not n:
        raise RuntimeError("TWILIO_FROM_NUMBER is missing.")
    return n

def allow_test_calls() -> bool:
    return (os.getenv("TWILIO_ALLOW_TEST_CALLS") or "false").strip().lower() == "true"

def get_test_to_number() -> str:
    n = (os.getenv("TWILIO_TEST_TO_NUMBER") or "").strip()
    if not n:
        raise RuntimeError("TWILIO_TEST_TO_NUMBER is missing.")
    return n

def _recording_webhook() -> str:
    env = (os.getenv("TWILIO_RECORDING_WEBHOOK") or "").strip()
    if env:
        return env
    return f"{_base_url()}/twilio/recording"

def _status_webhook() -> str:
    env = (os.getenv("TWILIO_CALL_STATUS_WEBHOOK") or "").strip()
    if env:
        return env
    return f"{_base_url()}/twilio/call/status"

def link_call_sid(call_sid: str, lead_id: int) -> None:
    if not call_sid:
        return
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO call_links (call_sid, lead_id)
                VALUES (:sid, :lead_id)
                ON CONFLICT (call_sid) DO UPDATE SET lead_id = EXCLUDED.lead_id
            """),
            {"sid": call_sid, "lead_id": lead_id},
        )

def lead_id_for_call_sid(call_sid: str) -> int | None:
    if not call_sid:
        return None
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT lead_id FROM call_links WHERE call_sid = :sid LIMIT 1"),
            {"sid": call_sid},
        ).fetchone()
        return int(row[0]) if row else None

def send_alert_sms(message: str) -> None:
    client = get_twilio_client()
    from_number = get_from_number()
    to_number = (os.getenv("ALERT_PHONE_NUMBER") or "").strip()
    if not to_number:
        raise RuntimeError("ALERT_PHONE_NUMBER is missing.")
    if allow_test_calls():
        to_number = get_test_to_number()
    client.messages.create(body=message, from_=from_number, to=to_number)

def send_lead_sms(to_number: str, message: str) -> str:
    if not to_number:
        return ""
    client = get_twilio_client()
    from_number = get_from_number()
    if allow_test_calls():
        to_number = get_test_to_number()
    msg = client.messages.create(body=message, from_=from_number, to=to_number)
    return msg.sid or ""

def make_call_with_recording(to_number: str, lead_id: int) -> str:
    client = get_twilio_client()
    from_number = get_from_number()

    if allow_test_calls():
        to_number = get_test_to_number()

    twiml_url = f"{_base_url()}/twilio/voice/twiml?lead_id={lead_id}"

    call = client.calls.create(
        to=to_number,
        from_=from_number,
        url=twiml_url,
        method="GET",
        record=True,
        recording_channels="dual",
        recording_track="both",
        recording_status_callback=_recording_webhook(),
        recording_status_callback_event=["completed"],
        recording_status_callback_method="POST",
        status_callback=_status_webhook(),
        status_callback_event=["initiated", "ringing", "answered", "completed"],
        status_callback_method="POST",
    )

    link_call_sid(call.sid, lead_id)
    return call.sid
