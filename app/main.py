import os
import sys
import secrets
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
            admin_password = os.environ.get("PANEL_DEFAULT_PASSWORD") or secrets.token_urlsafe(12)
            creds_file = "/var/lib/dst-panel/admin_credentials.txt"
            try:
                os.makedirs(os.path.dirname(creds_file), exist_ok=True)
                with open(creds_file, "w") as f:
                    f.write(f"Default admin password: {admin_password}\n")
                    f.write("CHANGE IMMEDIATELY via Profile → Change Password\n")
                os.chmod(creds_file, 0o600)
            except Exception:
                print(f"[WARN] Default admin password: {admin_password} — CHANGE IMMEDIATELY", flush=True)
            admin = User(
                username="admin",
                password_hash=hash_password(admin_password),
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

ALLOWED_HOSTS = os.environ.get("PANEL_ALLOWED_HOSTS", "").strip()
if ALLOWED_HOSTS:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[h.strip() for h in ALLOWED_HOSTS.split(",")],
    )
else:
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


@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False)
async def index():
    return FileResponse(str(TEMPLATES_DIR / "index.html"))


@app.get("/{path:path}")
async def spa_fallback(path: str):
    file_path = TEMPLATES_DIR / path
    if file_path.exists() and file_path.is_file() and file_path.suffix == ".html":
        return FileResponse(str(file_path))
    return FileResponse(str(TEMPLATES_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PANEL_PORT", "8000"))
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
