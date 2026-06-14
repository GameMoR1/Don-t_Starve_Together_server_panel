from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import Session as DBSession

from app.models.models import get_engine
from app.security.auth import get_user_by_session, check_role, log_audit_standalone
from app.services.world_library import (
    list_worlds,
    get_world,
    get_world_readiness,
    create_world,
    import_current_world,
    update_world,
    capture_world_from_current,
    delete_world,
    set_active_selection,
    get_active_selection,
    scan_current_saves,
)

router = APIRouter(prefix="/api/worlds", tags=["worlds"])


def _get_user(request: Request):
    session_id = request.cookies.get("session_id")
    engine = get_engine()
    with DBSession(engine) as db:
        user = get_user_by_session(db, session_id)
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return user


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else None


class CreateWorldRequest(BaseModel):
    name: str
    description: str = ""
    from_current: bool = False
    activate: bool = True


class UpdateWorldRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class ActiveWorldRequest(BaseModel):
    mode: str  # current | library | new
    world_id: Optional[str] = None


class ImportCurrentRequest(BaseModel):
    name: str
    description: str = ""
    activate: bool = True


@router.get("")
def worlds_list(request: Request):
    _get_user(request)
    return list_worlds()


@router.get("/readiness")
def worlds_readiness(request: Request, world_id: Optional[str] = None):
    _get_user(request)
    return get_world_readiness(world_id)


@router.get("/current")
def worlds_current(request: Request):
    _get_user(request)
    return {
        "active": get_active_selection(),
        "on_disk": scan_current_saves(),
    }


@router.get("/{world_id}")
def world_detail(world_id: str, request: Request):
    _get_user(request)
    world = get_world(world_id)
    if not world:
        raise HTTPException(status_code=404, detail="World not found")
    return {"world": world, "readiness": get_world_readiness(world_id)}


@router.post("")
def world_create(req: CreateWorldRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = create_world(
        req.name,
        description=req.description,
        from_current=req.from_current,
        activate=req.activate,
    )
    if result.get("success"):
        log_audit_standalone(user.id, "create_world", ip=_client_ip(request))
    return result


@router.post("/import-current")
def world_import_current(req: ImportCurrentRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = import_current_world(
        req.name,
        description=req.description,
        activate=req.activate,
    )
    if result.get("success"):
        log_audit_standalone(user.id, "import_current_world", ip=_client_ip(request))
    return result


@router.put("/{world_id}")
def world_update(world_id: str, req: UpdateWorldRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = update_world(world_id, name=req.name, description=req.description)
    if result.get("success"):
        log_audit_standalone(user.id, f"update_world_{world_id}", ip=_client_ip(request))
    return result


@router.post("/{world_id}/capture")
def world_capture(world_id: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = capture_world_from_current(world_id)
    if result.get("success"):
        log_audit_standalone(user.id, f"capture_world_{world_id}", ip=_client_ip(request))
    return result


@router.post("/active")
def world_set_active(req: ActiveWorldRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = set_active_selection(mode=req.mode, world_id=req.world_id)
    if result.get("success"):
        log_audit_standalone(user.id, f"set_active_world_{req.mode}", ip=_client_ip(request))
    return result


@router.delete("/{world_id}")
def world_delete(world_id: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = delete_world(world_id)
    if result.get("success"):
        log_audit_standalone(user.id, f"delete_world_{world_id}", ip=_client_ip(request))
    return result
