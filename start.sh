#!/usr/bin/env bash
set -o errexit
set -o nounset
set -o pipefail

export PYTHONPATH=/opt/render/project/src

exec uvicorn agencyvault_app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
