#!/usr/bin/env bash
set -euo pipefail

if [[ -f /opt/dst-panel/.env ]]; then
    set -a
    # shellcheck disable=SC1091
    source /opt/dst-panel/.env
    set +a
fi

HOST="${PANEL_HOST:-0.0.0.0}"
PORT="${PANEL_PORT:-8000}"
PYTHONPATH="${PYTHONPATH:-/opt/dst-panel}"
VENV_PYTHON="/opt/dst-panel/venv/bin/python"

export PYTHONPATH

exec "$VENV_PYTHON" -m uvicorn app.main:app \
    --host "$HOST" \
    --port "$PORT" \
    --proxy-headers \
    --forwarded-allow-ips='127.0.0.1'
