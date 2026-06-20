#!/usr/bin/env bash
set -euo pipefail

PANEL_DIR="/opt/dst-panel"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root"
    exit 1
fi

if [[ ! -d "${PANEL_DIR}" ]]; then
    log_error "Panel directory not found: ${PANEL_DIR}"
    exit 1
fi

if [[ ! -f "${PANEL_DIR}/venv/bin/python" ]]; then
    log_error "Python virtualenv not found at ${PANEL_DIR}/venv"
    exit 1
fi

log_info "Resetting admin password to admin123..."

"${PANEL_DIR}/venv/bin/python" -c "
import sys
sys.path.insert(0, '${PANEL_DIR}')
import hashlib, secrets

pw = 'admin123'
salt = secrets.token_hex(16)
dk = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 260000)
hashed = f'pbkdf2_sha256\$260000\${salt}\${dk.hex()}'

from app.models.models import get_engine, User
from sqlmodel import Session as DBSession, select

engine = get_engine()
with DBSession(engine) as db:
    user = db.exec(select(User).where(User.username == 'admin')).first()
    if user:
        user.password_hash = hashed
        user.login_attempts = 0
        user.locked_until = None
        db.add(user)
        db.commit()
        print('OK: admin password reset to admin123')
    else:
        print('ERROR: admin user not found')
"

log_info "Done! Login with: admin / admin123"
