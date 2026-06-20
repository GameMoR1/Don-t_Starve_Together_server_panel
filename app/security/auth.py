import os
import re
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

import pyotp
from itsdangerous import URLSafeTimedSerializer
from fastapi import Request, HTTPException

from app.models.models import User, UserSession, AuditLog, get_engine
from sqlmodel import Session as DBSession, select


ALGORITHM = "pbkdf2_sha256"
SESSION_DURATION_HOURS = 24
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
ENFORCE_SESSION_IP = os.environ.get("ENFORCE_SESSION_IP", "true").lower() == "true"

_SECRET_KEY = os.environ.get("PANEL_SECRET_KEY", "")
if not _SECRET_KEY:
    _SECRET_KEY = secrets.token_hex(32)
    os.environ["PANEL_SECRET_KEY"] = _SECRET_KEY
_serializer = URLSafeTimedSerializer(_SECRET_KEY)


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    iterations = 260000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    parts = hashed.split("$")
    if parts[0] != "pbkdf2_sha256":
        return False
    iterations = int(parts[1])
    salt = parts[2]
    stored_hash = parts[3]
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return secrets.compare_digest(dk.hex(), stored_hash)


def validate_password_strength(password: str) -> Optional[str]:
    if len(password) < 8:
        return "Password must be at least 8 characters"
    if not re.search(r"[A-Z]", password):
        return "Password must contain an uppercase letter"
    if not re.search(r"[a-z]", password):
        return "Password must contain a lowercase letter"
    if not re.search(r"[0-9]", password):
        return "Password must contain a digit"
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>_\-+]", password):
        return "Password must contain a special character"
    return None


def create_session(db: DBSession, user_id: int, ip_address: Optional[str] = None) -> UserSession:
    token = _serializer.dumps({"user_id": user_id})
    session = UserSession(
        id=token,
        user_id=user_id,
        expires_at=datetime.utcnow() + timedelta(hours=SESSION_DURATION_HOURS),
        ip_address=ip_address,
    )
    db.add(session)
    db.commit()
    return session


def get_user_by_session(db: DBSession, session_id: str, request_ip: Optional[str] = None) -> Optional[User]:
    if not session_id:
        return None
    try:
        data = _serializer.loads(session_id, max_age=SESSION_DURATION_HOURS * 3600)
        user_id = data.get("user_id")
        if user_id is not None:
            user = db.exec(select(User).where(User.id == user_id)).first()
            if user and ENFORCE_SESSION_IP and request_ip:
                stmt = select(UserSession).where(
                    UserSession.id == session_id,
                    UserSession.ip_address == request_ip,
                )
                if not db.exec(stmt).first():
                    return user if not ENFORCE_SESSION_IP else None
            return user
    except Exception:
        pass
    stmt = select(UserSession).where(
        UserSession.id == session_id, UserSession.expires_at > datetime.utcnow()
    )
    if ENFORCE_SESSION_IP and request_ip:
        stmt = stmt.where(UserSession.ip_address == request_ip)
    session = db.exec(stmt).first()
    if not session:
        return None
    stmt = select(User).where(User.id == session.user_id)
    return db.exec(stmt).first()


def check_role(user: User, required_role: str) -> bool:
    role_hierarchy = {"viewer": 0, "operator": 1, "admin": 2, "owner": 3}
    return role_hierarchy.get(user.role, -1) >= role_hierarchy.get(required_role, 0)


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def get_totp_uri(secret: str, username: str) -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name="DST Panel")


def verify_totp(secret: str, code: str) -> bool:
    totp = pyotp.TOTP(secret)
    return totp.verify(code)


def require_user(request: Request) -> User:
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid session")
        return user


def log_audit(db: DBSession, user_id: int, action: str, details: str = None, ip: str = None):
    log = AuditLog(user_id=user_id, action=action, details=details, ip_address=ip)
    db.add(log)
    db.commit()


def log_audit_standalone(user_id: int, action: str, details: str = None, ip: str = None):
    engine = get_engine()
    with DBSession(engine) as db:
        log_audit(db, user_id, action, details=details, ip=ip)


def check_login_lockout(db: DBSession, user: User) -> Optional[str]:
    if user.locked_until and user.locked_until > datetime.utcnow():
        remaining = int((user.locked_until - datetime.utcnow()).total_seconds()) // 60
        return f"Account locked. Try again in {remaining} minutes"
    return None


def handle_failed_login(db: DBSession, user: User):
    user.login_attempts += 1
    if user.login_attempts >= MAX_LOGIN_ATTEMPTS:
        user.locked_until = datetime.utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
    db.add(user)
    db.commit()


def reset_login_attempts(db: DBSession, user: User):
    user.login_attempts = 0
    user.locked_until = None
    db.add(user)
    db.commit()
