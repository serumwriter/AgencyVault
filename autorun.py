import time
from ai_tasks import ensure_ai_tasks_table
import requests
import os

ensure_ai_tasks_table()

AI_ENABLED = os.getenv("AI_AUTOMATIONS_ENABLED", "false").lower() == "true"
DRY_RUN = os.getenv("AI_DRY_RUN", "true").lower() == "true"
 = os.getenv("PUBLIC_BASE_URL")

if not BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL not set")

AI_RUN_URL = BASE_URL.rstrip("/") + "/ai/run"

def run_forever():
    print(">>> AI AUTORUN STARTED <<<")

    while True:
        try:
            if not AI_ENABLED:
    print("AI disabled — idle cycle")
else:
    print("Running AI cycle (dry_run =", DRY_RUN, ")")
    r = requests.get(AI_RUN_URL, timeout=30)
    print("AI response:", r.status_code)

        except Exception as e:
            print("AI autorun error:", e)

        # sleep 10 minutes
        time.sleep(600)

if __name__ == "__main__":
    run_forever()
