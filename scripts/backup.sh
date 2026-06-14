#!/usr/bin/env bash
set -euo pipefail

PANEL_DIR="/opt/dst-panel"
BACKUP_DIR="/var/lib/dst-panel/backups"
RETENTION_DAYS=30

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root"
    exit 1
fi

mkdir -p "${BACKUP_DIR}"

if [[ -f "${PANEL_DIR}/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "${PANEL_DIR}/.env"
    set +a
fi

export DST_DIR="${DST_DIR:-/home/dstpanel/dst}"
export DST_USER="${DST_USER:-dstpanel}"
export DATABASE_PATH="${DATABASE_PATH:-/var/lib/dst-panel/data.db}"
export DST_PANEL_DB="${DST_PANEL_DB:-$DATABASE_PATH}"
export PYTHONPATH="${PYTHONPATH:-${PANEL_DIR}}"

VENV_PY="${PANEL_DIR}/venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
    log_error "Panel venv not found: ${VENV_PY}"
    exit 1
fi

log_info "Stopping DST shards before backup..."
"${VENV_PY}" -c "
import asyncio, json, sys
sys.path.insert(0, '${PANEL_DIR}')
from app.config.config_reader import refresh_dst_paths
from app.services.dst_service import ensure_shards_stopped_for_backup

async def main():
    refresh_dst_paths()
    result = await ensure_shards_stopped_for_backup()
    if not result.get('ok'):
        print(json.dumps({'success': False, 'error': result.get('error')}))
        sys.exit(1)

asyncio.run(main())
" || {
    log_error "Failed to stop DST shards. Stop Master/Caves manually and retry."
    exit 1
}

log_info "Creating full DST backup via panel..."
RESULT=$("${VENV_PY}" -c "
import json, sys
sys.path.insert(0, '${PANEL_DIR}')
from app.config.config_reader import refresh_dst_paths
from app.backup.backup_manager import create_backup, save_backup_record

refresh_dst_paths()
result = create_backup()
if result.get('success'):
    save_backup_record(result)
print(json.dumps(result))
")

if ! echo "${RESULT}" | grep -q '"success": true'; then
    log_error "Backup failed: ${RESULT}"
    exit 1
fi

FILENAME=$(echo "${RESULT}" | "${VENV_PY}" -c "import json,sys; print(json.load(sys.stdin).get('filename',''))")
SIZE=$(echo "${RESULT}" | "${VENV_PY}" -c "import json,sys; print(json.load(sys.stdin).get('size_bytes',0))")

log_info "Backup created: ${BACKUP_DIR}/${FILENAME} (${SIZE} bytes)"

log_info "Rotating old backups (older than ${RETENTION_DAYS} days)..."
find "${BACKUP_DIR}" -name "dst_*.tar.gz" -mtime +"${RETENTION_DAYS}" -delete
find "${BACKUP_DIR}" -name "dst_*.tar.gz.sha256" -mtime +"${RETENTION_DAYS}" -delete

log_info "Backup complete!"
