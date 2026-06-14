import os
import asyncio
import tempfile

from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlmodel import Session as DBSession

from app.models.models import get_engine
from app.security.auth import get_user_by_session, check_role, log_audit_standalone
from app.backup.backup_manager import (
    create_backup,
    list_backups,
    restore_backup,
    delete_backup,
    import_uploaded_backup,
    get_backup_details,
    save_backup_record,
    _backup_path,
)
from app.services.dst_service import ensure_shards_stopped_for_backup

router = APIRouter(prefix="/api/backup", tags=["backups"])

MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


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


@router.post("/create")
async def create(request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    stop_result = await ensure_shards_stopped_for_backup()
    if not stop_result.get("ok"):
        raise HTTPException(status_code=409, detail=stop_result.get("error"))

    result = create_backup()
    if result.get("success"):
        save_backup_record(result)
    else:
        result["shards_stopped"] = True
        result["hint"] = (
            "Шарды уже остановлены. Исправьте ошибку и создайте бэкап снова "
            "или запустите кластер вручную."
        )
    log_audit_standalone(user.id, "create_backup", ip=_client_ip(request))
    if stop_result.get("stopped_orphans"):
        result["stopped_orphans"] = stop_result["stopped_orphans"]
    return result


@router.get("/list")
def list_backups_api(request: Request):
    _get_user(request)
    return {"backups": list_backups()}


@router.get("/download/{filename}")
def download(filename: str, request: Request):
    _get_user(request)
    path = _backup_path(filename)
    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Backup not found")
    return FileResponse(
        path,
        filename=filename,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    user = _get_user(request)
    if not check_role(user, "owner"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not file.filename or not (
        file.filename.endswith(".tar.gz") or file.filename.endswith(".tgz")
    ):
        raise HTTPException(status_code=400, detail="Нужен файл .tar.gz")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
            tmp_path = tmp.name
            total = 0
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Файл слишком большой (макс. {MAX_UPLOAD_BYTES // (1024**3)} ГБ)",
                    )
                tmp.write(chunk)

        result = import_uploaded_backup(tmp_path, file.filename)
        tmp_path = None
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Upload failed"))
        save_backup_record(result)
        log_audit_standalone(user.id, "upload_backup", ip=_client_ip(request))
        return result
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


@router.get("/details/{filename}")
def details(filename: str, request: Request):
    _get_user(request)
    result = get_backup_details(filename)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Not found"))
    return result


@router.post("/restore/{filename}")
async def restore(filename: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "owner"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    stop_result = await ensure_shards_stopped_for_backup()
    if not stop_result.get("ok"):
        raise HTTPException(status_code=409, detail=stop_result.get("error"))

    result = restore_backup(filename)
    log_audit_standalone(user.id, f"restore_backup_{filename}", ip=_client_ip(request))
    if not result.get("success"):
        result["shards_stopped"] = True
        raise HTTPException(status_code=400, detail=result.get("error", "Restore failed"))
    return result


@router.delete("/{filename}")
def delete(filename: str, request: Request):
    user = _get_user(request)
    if not check_role(user, "admin"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    result = delete_backup(filename)
    log_audit_standalone(user.id, f"delete_backup_{filename}", ip=_client_ip(request))
    return result
