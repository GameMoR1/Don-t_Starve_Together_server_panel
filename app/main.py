import os
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.models import get_engine, User
from app.security.auth import hash_password
from sqlmodel import Session as DBSession, select

from app.api import auth, server, config_api, backups, metrics, audit, worlds
from app.config.config_reader import restore_cluster_token_if_missing, ensure_shard_link_config, refresh_dst_paths

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    with DBSession(engine) as db:
        existing = db.exec(select(User).where(User.username == "admin")).first()
        if not existing:
            admin = User(
                username="admin",
                password_hash=hash_password("admin123"),
                role="owner",
            )
            db.add(admin)
            db.commit()
    restore_cluster_token_if_missing()
    refresh_dst_paths()
    try:
        ensure_shard_link_config()
    except Exception:
        pass
    try:
        from app.services.shard_registry import reconcile_registry
        reconcile_registry()
    except Exception:
        pass
    yield


app = FastAPI(
    title="DST Panel",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(auth.router)
app.include_router(server.router)
app.include_router(config_api.router)
app.include_router(backups.router)
app.include_router(worlds.router)
app.include_router(metrics.router)
app.include_router(audit.router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index():
    return FileResponse(str(TEMPLATES_DIR / "index.html"))


@app.get("/{path:path}")
async def spa_fallback(path: str):
    file_path = TEMPLATES_DIR / path
    if file_path.exists() and file_path.is_file():
        return FileResponse(str(file_path))
    return FileResponse(str(TEMPLATES_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PANEL_PORT", "8000"))
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
