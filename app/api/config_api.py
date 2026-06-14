from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlmodel import Session as DBSession

from app.models.models import get_engine
from app.security.auth import get_user_by_session, check_role, log_audit_standalone
from app.config.config_reader import (
    read_cluster_ini, write_cluster_ini,
    read_shard_ini, write_shard_ini,
    read_cluster_token, write_cluster_token, validate_cluster_token,
    get_caves_binding_status, bind_caves_to_master,
    get_master_binding_status, bind_master_to_cluster, get_cluster_binding_status,
    get_config_workflow_status, apply_cluster_bindings,
    read_text_file, write_text_file,
    CLUSTER_DIR,
)
from app.services.mod_service import (
    get_mods_overview, add_mod, add_collection, remove_mod,
    sync_mod_files_from_overrides,
)
from app.services.list_service import read_all_lists, apply_list_action, add_to_list
from app.services.player_service import get_players_overview

router = APIRouter(prefix="/api/config", tags=["config"])


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


class ClusterConfigUpdate(BaseModel):
    data: dict


class ShardConfigUpdate(BaseModel):
    data: dict


class ModOverrideUpdate(BaseModel):
    content: str


class ModAddRequest(BaseModel):
    workshop_id: str


class ModCollectionRequest(BaseModel):
    collection_id: str


class ListActionRequest(BaseModel):
    klei_id: str
    action: str


class ListAddRequest(BaseModel):
    klei_id: str
    list_type: str


class TokenUpdate(BaseModel):
    token: str


class TextFileUpdate(BaseModel):
    content: str


@router.get("/cluster")
def get_cluster(request: Request):
    _get_user(request)
    return read_cluster_ini()


@router.put("/cluster")
def update_cluster(req: ClusterConfigUpdate, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = write_cluster_ini(req.data)
    log_audit_standalone(user.id, "update_cluster_ini", ip=_client_ip(request))
    return result


@router.get("/shard/{name}")
def get_shard(name: str, request: Request):
    _get_user(request)
    return read_shard_ini(name)


@router.put("/shard/{name}")
def update_shard(name: str, req: ShardConfigUpdate, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = write_shard_ini(name, req.data)
    log_audit_standalone(user.id, f"update_shard_{name}", ip=_client_ip(request))
    return result


@router.get("/binding")
def cluster_binding(request: Request):
    _get_user(request)
    return get_cluster_binding_status()


@router.get("/workflow")
def config_workflow(request: Request):
    _get_user(request)
    return get_config_workflow_status()


@router.post("/apply-bindings")
def apply_bindings(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = apply_cluster_bindings()
    if result.get("success"):
        log_audit_standalone(user.id, "apply_cluster_bindings", ip=_client_ip(request))
    return result


@router.get("/caves/binding")
def caves_binding(request: Request):
    _get_user(request)
    return get_caves_binding_status()


@router.post("/caves/bind-master")
def caves_bind_master(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = bind_caves_to_master()
    if result.get("success"):
        log_audit_standalone(user.id, "bind_caves_master", ip=_client_ip(request))
    return result


@router.get("/master/binding")
def master_binding(request: Request):
    _get_user(request)
    return get_master_binding_status()


@router.post("/master/bind-cluster")
def master_bind_cluster(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = bind_master_to_cluster()
    if result.get("success"):
        log_audit_standalone(user.id, "bind_master_cluster", ip=_client_ip(request))
    return result


@router.get("/mods")
def get_mods(request: Request):
    _get_user(request)
    return get_mods_overview()


@router.put("/mods")
def update_mods(req: ModOverrideUpdate, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = sync_mod_files_from_overrides(req.content)
    if result.get("success"):
        log_audit_standalone(user.id, "update_modoverrides", ip=_client_ip(request))
    return result


@router.post("/mods/add")
def mods_add(req: ModAddRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = add_mod(req.workshop_id)
    if result.get("success"):
        log_audit_standalone(user.id, f"add_mod_{req.workshop_id}", ip=_client_ip(request))
    return result


@router.post("/mods/collection")
def mods_collection(req: ModCollectionRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = add_collection(req.collection_id)
    if result.get("success"):
        log_audit_standalone(user.id, f"add_collection_{req.collection_id}", ip=_client_ip(request))
    return result


@router.delete("/mods/{workshop_id}")
def mods_remove(workshop_id: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = remove_mod(workshop_id)
    if result.get("success"):
        log_audit_standalone(user.id, f"remove_mod_{workshop_id}", ip=_client_ip(request))
    return result


@router.get("/lists")
def get_lists(request: Request):
    _get_user(request)
    overview = get_players_overview()
    return {
        **read_all_lists(),
        "players": overview["players"],
        "online": overview["online"],
        "online_count": overview["online_count"],
        "log_available": overview["log_available"],
        "note": overview["note"],
    }


@router.post("/lists/action")
def lists_action(req: ListActionRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = apply_list_action(req.klei_id, req.action)
    if result.get("success"):
        log_audit_standalone(
            user.id, f"list_{req.action}_{req.klei_id}", ip=_client_ip(request)
        )
    return result


@router.post("/lists/add")
def lists_add(req: ListAddRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = add_to_list(req.list_type, req.klei_id)
    if result.get("success"):
        log_audit_standalone(
            user.id, f"list_add_{req.list_type}_{req.klei_id}", ip=_client_ip(request)
        )
    return result


@router.get("/token")
def get_token(request: Request):
    _get_user(request)
    token = read_cluster_token()
    cluster = read_cluster_ini()
    offline = cluster.get("NETWORK.offline_cluster", "false").lower() == "true"
    token_path = f"{CLUSTER_DIR}/cluster_token.txt"
    return {
        "has_token": bool(token),
        "token": "",
        "required": not offline,
        "offline_mode": offline,
        "klei_url": "https://accounts.klei.com/account/game/servers",
        "token_path": token_path,
    }


@router.put("/token")
def update_token(req: TokenUpdate, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    err = validate_cluster_token(req.token)
    if err:
        raise HTTPException(status_code=400, detail=err)
    result = write_cluster_token(req.token)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to save token"))
    log_audit_standalone(user.id, "update_token", ip=_client_ip(request))
    return result


@router.get("/file/{filename:path}")
def get_file(filename: str, request: Request):
    _get_user(request)
    filepath = f"{CLUSTER_DIR}/{filename}"
    content = read_text_file(filepath)
    if content is None:
        raise HTTPException(status_code=403, detail="Path not allowed")
    return {"content": content}


@router.put("/file/{filename:path}")
def update_file(filename: str, req: TextFileUpdate, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    filepath = f"{CLUSTER_DIR}/{filename}"
    result = write_text_file(filepath, req.content)
    log_audit_standalone(user.id, f"update_file_{filename}", ip=_client_ip(request))
    return result
