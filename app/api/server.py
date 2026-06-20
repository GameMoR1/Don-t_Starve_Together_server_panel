from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session as DBSession

from app.models.models import get_engine
from app.security.auth import get_user_by_session, check_role, log_audit_standalone
from app.services.dst_service import (
    start_shard, stop_shard, restart_shard, get_server_overview,
    install_dst_server, update_server, get_logs, create_cluster_structure,
    get_shard_prerequisites, get_shard_status,
    start_cluster_shards, stop_cluster_shards, restart_cluster_shards,
    start_regenerate_world, get_regenerate_world_status,
)
from app.services.setup_service import (
    get_setup_status, init_cluster, init_friends_cluster, init_online_cluster,
    repair_shard_link,
)
from app.services.player_service import get_players_overview, get_player_detail

router = APIRouter(prefix="/api/server", tags=["server"])


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


class InitClusterRequest(BaseModel):
    cluster_name: str = "Мой DST сервер"
    password: str = ""
    game_mode: str = "survival"


class FriendsPresetRequest(BaseModel):
    cluster_name: str = "Игра с друзьями"
    password: str = ""
    game_mode: str = "survival"


class OnlinePresetRequest(BaseModel):
    cluster_name: str = "Мой DST сервер"
    password: str = ""
    game_mode: str = "survival"


@router.get("/setup")
def setup_status(request: Request):
    _get_user(request)
    master = get_shard_status("Master")
    caves = get_shard_status("Caves")
    return get_setup_status(
        master_running=master.get("running", False),
        caves_running=caves.get("running", False),
    )


@router.post("/setup/repair-shards")
def setup_repair_shards(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = repair_shard_link()
    log_audit_standalone(user.id, "repair_shard_link", ip=_client_ip(request))
    return result


@router.post("/init-cluster")
def setup_init_cluster(req: InitClusterRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = init_cluster(req.cluster_name, req.password, req.game_mode)
    if result.get("success"):
        log_audit_standalone(user.id, "init_cluster", ip=_client_ip(request))
    return result


@router.post("/preset/friends")
def setup_friends_preset(req: FriendsPresetRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = init_friends_cluster(req.cluster_name, req.password, req.game_mode)
    if result.get("success"):
        log_audit_standalone(user.id, "friends_preset", ip=_client_ip(request))
    return result


@router.post("/preset/online")
def setup_online_preset(req: OnlinePresetRequest, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = init_online_cluster(req.cluster_name, req.password, req.game_mode)
    if result.get("success"):
        log_audit_standalone(user.id, "online_preset", ip=_client_ip(request))
    return result


@router.get("/status")
def status(request: Request):
    _get_user(request)
    overview = get_server_overview()
    try:
        players = get_players_overview()
        overview["players_online"] = players.get("online_count", 0)
        overview["online_players"] = players.get("online", [])
    except Exception:
        overview["players_online"] = 0
        overview["online_players"] = []
    return overview


@router.get("/readiness/{shard}")
def readiness(shard: str, request: Request):
    _get_user(request)
    return get_shard_prerequisites(shard)


@router.post("/start/{shard}")
async def start(shard: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await start_shard(shard)
    log_audit_standalone(user.id, f"start_{shard}", ip=_client_ip(request))
    return result


@router.post("/stop/{shard}")
async def stop(shard: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await stop_shard(shard)
    log_audit_standalone(user.id, f"stop_{shard}", ip=_client_ip(request))
    return result


@router.post("/restart/{shard}")
async def restart(shard: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await restart_shard(shard)
    log_audit_standalone(user.id, f"restart_{shard}", ip=_client_ip(request))
    return result


@router.get("/cluster-readiness")
def cluster_readiness(request: Request):
    _get_user(request)
    from app.config.config_reader import validate_cluster_config
    return validate_cluster_config()


@router.post("/start-cluster")
async def start_cluster(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await start_cluster_shards()
    log_audit_standalone(user.id, "start_cluster", ip=_client_ip(request))
    return result


@router.post("/stop-cluster")
async def stop_cluster(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await stop_cluster_shards()
    log_audit_standalone(user.id, "stop_cluster", ip=_client_ip(request))
    return result


@router.post("/restart-cluster")
async def restart_cluster(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await restart_cluster_shards()
    log_audit_standalone(user.id, "restart_cluster", ip=_client_ip(request))
    return result


@router.post("/regenerate-world")
async def regenerate_world(request: Request):
    user = _get_user(request)
    if not check_role(user, "operator"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await start_regenerate_world()
    if result.get("success"):
        log_audit_standalone(user.id, "regenerate_world", ip=_client_ip(request))
    return result


@router.get("/regenerate-world/status")
def regenerate_world_status(request: Request):
    _get_user(request)
    return get_regenerate_world_status()


@router.post("/install")
async def install(request: Request):
    user = _get_user(request)
    if not check_role(user, "owner"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await install_dst_server()
    log_audit_standalone(user.id, "install_server", ip=_client_ip(request))
    return result


@router.post("/update")
async def update(request: Request):
    user = _get_user(request)
    if not check_role(user, "owner"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await update_server()
    log_audit_standalone(user.id, "update_server", ip=_client_ip(request))
    return result


@router.get("/logs/{shard}")
def logs(shard: str, request: Request, lines: int = Query(100, ge=10, le=5000)):
    _get_user(request)
    return get_logs(shard, lines)


@router.get("/players")
def players(request: Request):
    _get_user(request)
    return get_players_overview()


@router.get("/players/{klei_id}")
def player_detail(klei_id: str, request: Request):
    _get_user(request)
    detail = get_player_detail(klei_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Player not found")
    return detail


@router.delete("/process/{shard}")
async def kill_process(shard: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = await stop_shard(shard)
    if result.get("success"):
        log_audit_standalone(user.id, f"kill_{shard}", ip=_client_ip(request))
    return result
