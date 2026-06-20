import uuid
import os
import base64
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Optional
from sqlmodel import Field, SQLModel, create_engine


def _fernet_key_from_secret() -> bytes:
    key = os.environ.get("PANEL_SECRET_KEY", "")
    if not key:
        key = hashlib.sha256(b"dst-panel-fallback").hexdigest()
    return base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())


def encrypt_totp_secret(secret: str) -> str:
    from cryptography.fernet import Fernet
    f = Fernet(_fernet_key_from_secret())
    return f.encrypt(secret.encode()).decode()


def decrypt_totp_secret(encrypted: str) -> Optional[str]:
    try:
        from cryptography.fernet import Fernet
        f = Fernet(_fernet_key_from_secret())
        return f.decrypt(encrypted.encode()).decode()
    except Exception:
        return None


class User(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    username: str = Field(unique=True, index=True)
    password_hash: str
    role: str = Field(default="admin")
    totp_secret: Optional[str] = None
    totp_enabled: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    login_attempts: int = 0
    locked_until: Optional[datetime] = None


class UserSession(SQLModel, table=True):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    ip_address: Optional[str] = None


class AuditLog(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="user.id")
    action: str
    details: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Backup(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    filename: str
    size_bytes: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    checksum: Optional[str] = None


class Mod(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    workshop_id: str = Field(unique=True, index=True)
    enabled: bool = True
    name: Optional[str] = None
    mod_type: str = Field(default="both")  # server, client, both
    last_checked: Optional[datetime] = None
    notes: Optional[str] = None


class ServerState(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    key: str = Field(unique=True, index=True)
    value: str


class PlayerRecord(SQLModel, table=True):
    klei_id: str = Field(primary_key=True, index=True)
    name: Optional[str] = None
    first_seen: Optional[datetime] = None
    last_seen: Optional[datetime] = None
    last_join: Optional[datetime] = None
    last_leave: Optional[datetime] = None
    first_ip: Optional[str] = None
    last_ip: Optional[str] = None
    join_count: int = 0
    session_count: int = 0
    total_playtime_seconds: int = 0
    is_online: bool = False


class PlayerSession(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    klei_id: str = Field(index=True)
    name: Optional[str] = None
    started_at: datetime = Field(default_factory=datetime.utcnow, index=True)
    ended_at: Optional[datetime] = None
    ip_address: Optional[str] = None
    shard: str = "Master"
    duration_seconds: int = 0


def _default_db_path() -> str:
    for key in ("DST_PANEL_DB", "DATABASE_PATH"):
        env_path = os.environ.get(key)
        if env_path:
            return env_path
    if os.name == "nt":
        return str(Path.home() / ".dst-panel" / "data.db")
    return "/var/lib/dst-panel/data.db"


sqlite_file = _default_db_path()
engine = None


def _migrate_player_schema(engine) -> None:
    from sqlalchemy import text

    with engine.connect() as conn:
        try:
            cols = {
                row[1]
                for row in conn.execute(text("PRAGMA table_info(playerrecord)")).fetchall()
            }
        except Exception:
            cols = set()

        additions = {
            "first_ip": "TEXT",
            "last_ip": "TEXT",
            "session_count": "INTEGER DEFAULT 0",
            "total_playtime_seconds": "INTEGER DEFAULT 0",
        }
        for col, col_type in additions.items():
            if col not in cols:
                try:
                    conn.execute(text(f"ALTER TABLE playerrecord ADD COLUMN {col} {col_type}"))
                except Exception:
                    pass
        conn.commit()


def get_engine(db_path: str = None):
    global engine
    if engine is None:
        path = db_path or sqlite_file
        db_dir = os.path.dirname(path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}", echo=False)
        SQLModel.metadata.create_all(engine)
        _migrate_player_schema(engine)
    return engine


def get_db_session(db_path: str = None):
    from sqlmodel import Session as DBSession
    eng = get_engine(db_path)
    return DBSession(eng)
