from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlmodel import select

from app.models.models import User, UserSession, get_engine
from sqlmodel import Session as DBSession
from app.security.auth import (
    hash_password, verify_password, create_session, get_user_by_session,
    check_role, generate_totp_secret, get_totp_uri, verify_totp,
    log_audit, check_login_lockout, handle_failed_login,
    reset_login_attempts, validate_password_strength,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


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
    engine = get_engine()
    with DBSession(engine) as db:
        stmt = select(User).where(User.username == req.username)
        user = db.exec(stmt).first()

        if not user:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        lock_msg = check_login_lockout(db, user)
        if lock_msg:
            raise HTTPException(status_code=423, detail=lock_msg)

        if not verify_password(req.password, user.password_hash):
            handle_failed_login(db, user)
            raise HTTPException(status_code=401, detail="Invalid credentials")

        if user.totp_enabled:
            if not req.totp_code or not verify_totp(user.totp_secret, req.totp_code):
                raise HTTPException(status_code=401, detail="Invalid 2FA code")

        reset_login_attempts(db, user)
        session = create_session(db, user.id)
        ip = request.client.host if request.client else None
        log_audit(db, user.id, "login", f"Login from {ip or 'unknown'}", ip)

        return LoginResponse(success=True, session_id=session.id)


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
        user = get_user_by_session(db, session_id)
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
        user = get_user_by_session(db, session_id)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not check_role(user, "admin"):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        if not verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid password")

        secret = generate_totp_secret()
        user.totp_secret = secret
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
        user = get_user_by_session(db, session_id)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not user.totp_secret:
            raise HTTPException(status_code=400, detail="2FA not initialized")
        if not verify_totp(user.totp_secret, req.code):
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
        user = get_user_by_session(db, session_id)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not verify_password(req.current_password, user.password_hash):
            raise HTTPException(status_code=401, detail="Invalid current password")
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
        user = get_user_by_session(db, session_id)
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
        user = get_user_by_session(db, session_id)
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
        user = get_user_by_session(db, session_id)
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
        user = get_user_by_session(db, session_id)
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
