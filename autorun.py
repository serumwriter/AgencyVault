import time
import requests
import os

BASE_URL = os.getenv("PUBLIC_BASE_URL")

if not BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL not set")

AI_RUN_URL = BASE_URL.rstrip("/") + "/ai/run"

def run_forever():
    print(">>> AI AUTORUN STARTED <<<")

    while True:
        try:
            print("Running AI cycle...")
            r = requests.get(AI_RUN_URL, timeout=30)
            print("AI response:", r.status_code, r.text)
        except Exception as e:
            print("AI autorun error:", e)

        # sleep 10 minutes
        time.sleep(600)

if __name__ == "__main__":
    run_forever()
