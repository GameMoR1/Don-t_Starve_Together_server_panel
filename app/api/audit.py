from fastapi import APIRouter, Request, HTTPException, Query
from sqlmodel import Session as DBSession, select

from app.models.models import get_engine, AuditLog, User
from app.security.auth import get_user_by_session, check_role

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("/logs")
def get_audit_logs(request: Request, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not check_role(user, "admin"):
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
        logs = db.exec(stmt).all()
        result = []
        for log in logs:
            u = db.get(User, log.user_id)
            result.append({
                "id": log.id,
                "username": u.username if u else "unknown",
                "action": log.action,
                "details": log.details,
                "ip_address": log.ip_address,
                "created_at": log.created_at.isoformat(),
            })
        return {"logs": result, "total": len(result)}
