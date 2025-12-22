#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

exec uvicorn agencyvault_app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

