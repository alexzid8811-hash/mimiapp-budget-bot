#!/usr/bin/env bash
set -euo pipefail
export DEV_MODE="${DEV_MODE:-true}"
export DATABASE_PATH="${DATABASE_PATH:-./data/budget.sqlite3}"
exec uvicorn app.main:app --reload --host 0.0.0.0 --port "${PORT:-8000}"
