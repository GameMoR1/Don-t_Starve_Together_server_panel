from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from sqlmodel import select
from collections import defaultdict
import time

from app.models.models import User, UserSession, get_engine, encrypt_totp_secret, decrypt_totp_secret
from sqlmodel import Session as DBSession
from app.security.auth import (
    hash_password, verify_password, create_session, get_user_by_session,
    check_role, generate_totp_secret, get_totp_uri, verify_totp,
    log_audit, check_login_lockout, handle_failed_login,
    reset_login_attempts, validate_password_strength,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

_ip_attempts: dict = defaultdict(list)
IP_RATE_WINDOW = 300
IP_MAX_ATTEMPTS = 20


def _check_ip_rate(ip: str) -> Optional[str]:
    now = time.time()
    _ip_attempts[ip] = [t for t in _ip_attempts[ip] if now - t < IP_RATE_WINDOW]
    if len(_ip_attempts[ip]) >= IP_MAX_ATTEMPTS:
        retry_after = int(IP_RATE_WINDOW - (now - _ip_attempts[ip][0]))
        return f"Too many requests. Try again in {max(1, retry_after)} seconds"
    return None


def _record_ip_attempt(ip: str):
    _ip_attempts[ip].append(time.time())


class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str = ""


class LoginResponse(BaseModel):
    success: bool
    session_id: str = ""
    error: str = ""


class TOTPEnableRequest(BaseModel):
    password: str


class TOTPVerifyRequest(BaseModel):
    code: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UserCreateRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class UserUpdateRequest(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


@router.post("/login", response_model=LoginResponse)
def login(req: LoginRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    rate_msg = _check_ip_rate(ip)
    if rate_msg:
        raise HTTPException(status_code=429, detail=rate_msg)
    engine = get_engine()
    with DBSession(engine) as db:
        stmt = select(User).where(User.username == req.username)
        user = db.exec(stmt).first()

        if not user:
            _record_ip_attempt(ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        lock_msg = check_login_lockout(db, user)
        if lock_msg:
            raise HTTPException(status_code=423, detail=lock_msg)

        if not verify_password(req.password, user.password_hash):
            handle_failed_login(db, user)
            _record_ip_attempt(ip)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if user.totp_enabled:
            totp_plain = decrypt_totp_secret(user.totp_secret) if user.totp_secret else None
            if not req.totp_code or not totp_plain or not verify_totp(totp_plain, req.totp_code):
                _record_ip_attempt(ip)
                raise HTTPException(status_code=401, detail="Invalid 2FA code")

        reset_login_attempts(db, user)
        _ip_attempts.pop(ip, None)
        session = create_session(db, user.id, ip)
        log_audit(db, user.id, "login", f"Login from {ip or 'unknown'}", ip)

        response = JSONResponse(content={"success": True, "session_id": session.id})
        secure = request.headers.get("X-Forwarded-Proto", request.url.scheme) == "https"
        response.set_cookie(
            key="session_id",
            value=session.id,
            httponly=True,
            secure=secure,
            samesite="strict",
            max_age=86400,
            path="/",
        )
        return response


@router.post("/logout")
def logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id:
        engine = get_engine()
        with DBSession(engine) as db:
            stmt = select(UserSession).where(UserSession.id == session_id)
            db_session = db.exec(stmt).first()
            if db_session:
                db.delete(db_session)
                db.commit()
    return {"success": True}


@router.get("/me")
def get_me(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Invalid session")
        return {
            "username": user.username,
            "role": user.role,
            "totp_enabled": user.totp_enabled,
        }


@router.post("/2fa/enable")
def enable_2fa(req: TOTPEnableRequest, request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not check_role(user, "admin"):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if not verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid password")

        secret = generate_totp_secret()
        user.totp_secret = encrypt_totp_secret(secret)
        user.totp_enabled = False
        db.add(user)
        db.commit()

        uri = get_totp_uri(secret, user.username)
        return {"success": True, "secret": secret, "uri": uri}


@router.post("/2fa/verify")
def verify_2fa(req: TOTPVerifyRequest, request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not user.totp_secret:
            raise HTTPException(status_code=400, detail="2FA not initialized")
        totp_plain = decrypt_totp_secret(user.totp_secret)
        if not totp_plain or not verify_totp(totp_plain, req.code):
            raise HTTPException(status_code=401, detail="Invalid code")

        user.totp_enabled = True
        db.add(user)
        db.commit()
        return {"success": True}


@router.post("/password")
def change_password(req: ChangePasswordRequest, request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not verify_password(req.current_password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid current password")
        if req.current_password == req.new_password:
            raise HTTPException(status_code=400, detail="New password must differ from current password")
        err = validate_password_strength(req.new_password)
        if err:
            raise HTTPException(status_code=400, detail=err)
        user.password_hash = hash_password(req.new_password)
        db.add(user)
        db.commit()
        ip = request.client.host if request.client else None
        log_audit(db, user.id, "change_password", ip=ip)
        return {"success": True}


@router.get("/users")
def list_users(request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not check_role(user, "admin"):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        stmt = select(User)
        users = db.exec(stmt).all()
        return [
            {"id": u.id, "username": u.username, "role": u.role, "totp_enabled": u.totp_enabled}
            for u in users
        ]


@router.post("/users")
def create_user(req: UserCreateRequest, request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not check_role(user, "owner"):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        err = validate_password_strength(req.password)
        if err:
            raise HTTPException(status_code=400, detail=err)
        existing = db.exec(select(User).where(User.username == req.username)).first()
        if existing:
            raise HTTPException(status_code=400, detail="Username already exists")
        new_user = User(
            username=req.username,
            password_hash=hash_password(req.password),
            role=req.role,
        )
        db.add(new_user)
        db.commit()
        ip = request.client.host if request.client else None
        log_audit(db, user.id, f"create_user {req.username}", ip=ip)
        return {"success": True}


@router.put("/users/{user_id}")
def update_user(user_id: int, req: UserUpdateRequest, request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not check_role(user, "owner"):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if req.role:
            target.role = req.role
        if req.password:
            err = validate_password_strength(req.password)
            if err:
                raise HTTPException(status_code=400, detail=err)
            target.password_hash = hash_password(req.password)
        db.add(target)
        db.commit()
        ip = request.client.host if request.client else None
        log_audit(db, user.id, f"update_user {target.username}", ip=ip)
        return {"success": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id, request.client.host if request.client else None)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not check_role(user, "owner"):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(status_code=404, detail="User not found")
        if target.id == user.id:
            raise HTTPException(status_code=400, detail="Cannot delete yourself")
        db.delete(target)
        db.commit()
        ip = request.client.host if request.client else None
        log_audit(db, user.id, f"delete_user {target.username}", ip=ip)
        return {"success": True}
