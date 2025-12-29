import time
import requests
import os

from ai_tasks import ensure_ai_tasks_table

# initialize db tables safely
ensure_ai_tasks_table()

# safety switches
AI_ENABLED = os.getenv("AI_AUTOMATIONS_ENABLED", "false").lower() == "true"
DRY_RUN = os.getenv("AI_DRY_RUN", "true").lower() == "true"

BASE_URL = os.getenv("PUBLIC_BASE_URL")
if not BASE_URL:
    raise RuntimeError("PUBLIC_BASE_URL not set")

AI_RUN_URL = BASE_URL.rstrip("/") + "/ai/run"

def run_forever():
    print(">>> AI AUTORUN STARTED <<<")
    print("AI enabled:", AI_ENABLED)
    print("Dry run:", DRY_RUN)

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

        # short sleep to prevent CPU spinning
        time.sleep(2)

if __name__ == "__main__":
    run_forever()
