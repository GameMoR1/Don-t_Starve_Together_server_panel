from fastapi import APIRouter, Request, HTTPException
from sqlmodel import Session as DBSession

from app.security.auth import require_user

router = APIRouter(prefix="/api/metrics", tags=["metrics"])





@router.get("/system")
def system_metrics(request: Request):
    require_user(request)
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        net = psutil.net_io_counters()
        return {
            "cpu_percent": cpu,
            "ram_total": mem.total,
            "ram_used": mem.used,
            "ram_percent": mem.percent,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_percent": disk.percent,
            "net_bytes_sent": net.bytes_sent,
            "net_bytes_recv": net.bytes_recv,
        }
    except ImportError:
        return {"error": "psutil not available"}
    except Exception as e:
        return {"error": str(e)}


@router.get("/server")
def server_metrics(request: Request):
    require_user(request)
    from app.services.dst_service import get_shard_status
    return {
        "master": get_shard_status("Master"),
        "caves": get_shard_status("Caves"),
    }
