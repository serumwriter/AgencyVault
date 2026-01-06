import os
from twilio.rest import Client

def get_twilio_client() -> Client:
    account_sid = (os.environ.get("TWILIO_ACCOUNT_SID") or "").strip()
    auth_token = (os.environ.get("TWILIO_AUTH_TOKEN") or "").strip()
    if not account_sid or not auth_token:
        raise RuntimeError("Twilio credentials missing (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN).")
    return Client(account_sid, auth_token)

def get_from_number() -> str:
    from_number = (os.environ.get("TWILIO_FROM_NUMBER") or "").strip()
    if not from_number:
        raise RuntimeError("TWILIO_FROM_NUMBER is missing.")
    return from_number

def send_alert_sms(message: str):
    client = get_twilio_client()
    from_number = get_from_number()
    to_number = (os.environ.get("ALERT_PHONE_NUMBER") or "").strip()
    if not to_number:
        raise RuntimeError("ALERT_PHONE_NUMBER is missing.")
    client.messages.create(body=message, from_=from_number, to=to_number)

def send_lead_sms(to_number: str, message: str):
    if not to_number:
        return
    client = get_twilio_client()
    from_number = get_from_number()
    client.messages.create(body=message, from_=from_number, to=to_number)

def make_call(to_number: str, twiml_url: str):
    client = get_twilio_client()
    from_number = get_from_number()
    return client.calls.create(
        to=to_number,
        from_=from_number,
        url=twiml_url,
        record=True,
        recording_status_callback=(os.environ.get("TWILIO_RECORDING_WEBHOOK") or "").strip() or None,
        recording_status_callback_event=["completed"],
        recording_status_callback_method="POST",
    )
def make_call_with_recording(to: str, lead_id: int | None = None):
    """
    Wrapper to preserve backward compatibility.
    Calls the existing make_call() with recording enabled.
    """
    return make_call(to=to, lead_id=lead_id)
