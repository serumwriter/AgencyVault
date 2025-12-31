import os
from twilio.rest import Client


def get_twilio_client() -> Client:
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()

    if not account_sid or not auth_token:
        raise RuntimeError(
            "Twilio credentials missing (TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN)."
        )

    return Client(account_sid, auth_token)


def get_from_number() -> str:
    from_number = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
    if not from_number:
        raise RuntimeError("TWILIO_FROM_NUMBER is missing.")
    return from_number


def allow_test_calls() -> bool:
    return os.environ.get("TWILIO_ALLOW_TEST_CALLS", "false").strip().lower() == "true"


def get_test_to_number() -> str:
    to_number = os.environ.get("TWILIO_TEST_TO_NUMBER", "").strip()
    if not to_number:
        raise RuntimeError("TWILIO_TEST_TO_NUMBER is missing.")
    return to_number
def send_alert_sms(message: str):
    """
    Sends an SMS alert to the human operator when the AI determines
    human intervention is required.
    """
    client = get_twilio_client()
    from_number = get_from_number()

    # Primary human alert number
    to_number = os.environ.get("ALERT_PHONE_NUMBER", "").strip()
    if not to_number:
        raise RuntimeError("ALERT_PHONE_NUMBER is missing.")

    # Test mode override (safe development)
    if allow_test_calls():
        to_number = get_test_to_number()

    client.messages.create(
        body=message,
        from_=from_number,
        to=to_number,
    )
