#!/usr/bin/env bash
set -euo pipefail

export PYTHONUNBUFFERED=1

if [[ "${WORKER:-0}" == "1" ]]; then
  python -c "from agencyvault_app.executor import run_executor_loop; run_executor_loop()"
else
  uvicorn agencyvault_app.main:app --host 0.0.0.0 --port "${PORT:-10000}"
fi
