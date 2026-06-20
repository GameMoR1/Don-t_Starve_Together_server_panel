import os
import re
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from sqlmodel import Session as DBSession, select, delete

from app.config.config_reader import get_cluster_dir, refresh_dst_paths
from app.models.models import PlayerRecord, PlayerSession, ServerState, get_engine
from app.services.dst_service import normalize_shard
from app.services.list_service import get_player_roles, read_all_lists

RE_AUTH = re.compile(
    r"Client authenticated:\s*\((KU_[A-Za-z0-9_]+|OU_\d+)\)\s*(.*)",
    re.IGNORECASE,
)
RE_VALIDATE = re.compile(
    r"ValidateGameSessionToken.*?\^((?:KU_|OU_)[A-Za-z0-9_]+)\^",
    re.IGNORECASE,
)
RE_CONNECT = re.compile(
    r"Client connected from\s+([0-9a-fA-F:.]+)\|(\d+)",
    re.IGNORECASE,
)
RE_INCOMING = re.compile(
    r"New incoming connection\s+([0-9a-fA-F:.]+)\|(\d+)",
    re.IGNORECASE,
)
RE_JOIN_ANN = re.compile(r"\[Join Announcement\]\s*(.+?)\s*$", re.IGNORECASE)
RE_LEAVE_ANN = re.compile(r"\[Leave Announcement\]\s*(.+?)\s*$", re.IGNORECASE)
RE_CHAT = re.compile(
    r"\((KU_[A-Za-z0-9_]+|OU_\d+)\)\s+(.+?)\s+said:",
    re.IGNORECASE,
)
RE_SAVE = re.compile(
    r"Read save location file for\s*\((KU_[A-Za-z0-9_]+|OU_\d+)\)",
    re.IGNORECASE,
)
RE_STEAM_AUTH = re.compile(
    r"\[Steam\]\s+Authenticated\s+(?:host|client)\s+'(\d+)'",
    re.IGNORECASE,
)
RE_STEAM_DISCONNECT = re.compile(
    r"\[Steam\]\s+SendUserDisconnect\s+for\s+'(\d+)'",
    re.IGNORECASE,
)
RE_USER_OWNERSHIP = re.compile(
    r"User ID\s+(KU_[A-Za-z0-9_]+|OU_\d+)\s+assigned ownership",
    re.IGNORECASE,
)
RE_SPAWN = re.compile(
    r"Spawn request:\s+\S+\s+from\s+(.+?)\s*$",
    re.IGNORECASE,
)

_LOG_FILES = (
    ("Master", "server_log.txt"),
    ("Master", "server_chat_log.txt"),
    ("Caves", "server_log.txt"),
    ("Caves", "server_chat_log.txt"),
)

_RE_LOG_TS = re.compile(
    r"^\[(\d{1,2}:\d{2}:\d{2})(?::\d{2})?\]:",
)

_CACHE_TTL = 4
_overview_cache = {"at": 0.0, "data": None}
_SYNC_V3_KEY = "player_sync_v3_done"
_SYNC_V4_KEY = "player_sync_v4_reconcile"
_SYNC_V5_KEY = "player_sync_v5_reparse"
_SYNC_V6_KEY = "player_sync_v6_log_timestamps"
_STEAM_MAP_KEY = "player_steam_to_klei"

_SERVER_LOG_FILES = (
    ("Master", "server_log.txt"),
    ("Caves", "server_log.txt"),
)
_CHAT_LOG_FILES = (
    ("Master", "server_chat_log.txt"),
    ("Caves", "server_chat_log.txt"),
)


def invalidate_players_cache():
    _overview_cache["at"] = 0.0
    _overview_cache["data"] = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cluster_dir() -> str:
    return refresh_dst_paths()["cluster_dir"]


def _shard_log_path(shard: str, filename: str = "server_log.txt") -> str:
    shard = normalize_shard(shard)
    return os.path.join(_cluster_dir(), shard, filename)


def _read_log_lines(path: str, max_lines: int = 100000) -> List[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            file_size = f.tell()
            if file_size == 0:
                return []

            buf_size = 8192
            pos = file_size
            lines = []
            remainder = b""

            while len(lines) < max_lines and pos > 0:
                read_size = min(buf_size, pos)
                pos -= read_size
                f.seek(pos)
                chunk = f.read(read_size)

                blocks = chunk.split(b"\n")
                blocks[-1] = blocks[-1] + remainder
                remainder = blocks[0]
                lines = [b.decode("utf-8", errors="replace") for b in blocks[1:][::-1]] + lines

            if remainder:
                line = remainder.decode("utf-8", errors="replace")
                if line.strip():
                    lines.insert(0, line)

        return lines[-max_lines:]
    except Exception:
        return []


def _get_state(db: DBSession, key: str) -> str:
    row = db.exec(select(ServerState).where(ServerState.key == key)).first()
    return row.value if row else ""


def _set_state(db: DBSession, key: str, value: str) -> None:
    row = db.exec(select(ServerState).where(ServerState.key == key)).first()
    if row:
        row.value = value
        db.add(row)
    else:
        db.add(ServerState(key=key, value=value))


def _log_state_key(shard: str, filename: str) -> str:
    return f"player_log_lines:{shard}:{filename}"


def _read_new_log_lines(db: DBSession, shard: str, filename: str) -> List[str]:
    path = _shard_log_path(shard, filename)
    key = _log_state_key(shard, filename)
    all_lines = _read_log_lines(path)
    start = int(_get_state(db, key) or "0")
    if len(all_lines) < start:
        start = 0
    new_lines = all_lines[start:]
    _set_state(db, key, str(len(all_lines)))
    return new_lines


def _reset_log_offsets(db: DBSession) -> None:
    for shard, filename in _LOG_FILES:
        _set_state(db, _log_state_key(shard, filename), "0")


def _load_steam_map(db: DBSession) -> Dict[str, str]:
    raw = _get_state(db, _STEAM_MAP_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_steam_map(db: DBSession, mapping: Dict[str, str]) -> None:
    _set_state(db, _STEAM_MAP_KEY, json.dumps(mapping))


def _ensure_sync_version(db: DBSession, key_flag: str, next_keys: Optional[list] = None, reconcile: bool = False) -> None:
    if _get_state(db, key_flag) == "1":
        return
    db.exec(delete(PlayerSession))
    for record in db.exec(select(PlayerRecord)).all():
        record.is_online = False
        record.join_count = 0
        record.session_count = 0
        record.total_playtime_seconds = 0
        db.add(record)
    _reset_log_offsets(db)
    _set_state(db, key_flag, "1")
    if next_keys:
        for nk in next_keys:
            _set_state(db, nk, "")
    if reconcile:
        _reconcile_all_players(db)
    db.commit()


def _ensure_sync_v2(db: DBSession) -> None:
    _ensure_sync_version(db, _SYNC_V3_KEY, next_keys=[_SYNC_V4_KEY])
    if _get_state(db, _STEAM_MAP_KEY) != "{}":
        _set_state(db, _STEAM_MAP_KEY, "{}")
        db.commit()


def _ensure_sync_v5_reparse(db: DBSession) -> None:
    _ensure_sync_version(db, _SYNC_V5_KEY, next_keys=[_SYNC_V4_KEY, _SYNC_V6_KEY])


def _ensure_sync_v6_log_timestamps(db: DBSession) -> None:
    _ensure_sync_version(db, _SYNC_V6_KEY, next_keys=[_SYNC_V4_KEY])


def _ensure_sync_v4(db: DBSession) -> None:
    _ensure_sync_version(db, _SYNC_V4_KEY, reconcile=True)


def _session_duration(session: PlayerSession, now: Optional[datetime] = None) -> int:
    now = now or _utcnow()
    if not session.started_at:
        return 0
    if session.ended_at:
        if session.duration_seconds is not None:
            return max(0, int(session.duration_seconds))
        return max(0, int((session.ended_at - session.started_at).total_seconds()))
    return max(0, int((now - session.started_at).total_seconds()))


def _sessions_for_player(db: DBSession, klei_id: str) -> List[PlayerSession]:
    return list(
        db.exec(
            select(PlayerSession)
            .where(PlayerSession.klei_id == klei_id)
            .order_by(PlayerSession.started_at.asc())
        ).all()
    )


def _reconcile_player_stats(db: DBSession, klei_id: str, now: Optional[datetime] = None) -> None:
    now = now or _utcnow()
    sessions = _sessions_for_player(db, klei_id)
    record = db.exec(
        select(PlayerRecord).where(PlayerRecord.klei_id == klei_id)
    ).first()
    if not record:
        return

    open_sessions = [s for s in sessions if s.ended_at is None]
    if len(open_sessions) > 1:
        open_sessions.sort(key=lambda s: s.started_at or now)
        for stale in open_sessions[:-1]:
            stale.ended_at = open_sessions[-1].started_at or now
            stale.duration_seconds = _session_duration(stale, stale.ended_at)
            db.add(stale)
        open_sessions = open_sessions[-1:]

    total_playtime = sum(_session_duration(s, now) for s in sessions)
    record.total_playtime_seconds = total_playtime
    record.session_count = len(sessions)
    record.is_online = bool(open_sessions)
    if open_sessions:
        record.last_join = open_sessions[-1].started_at
    if sessions:
        last_end = max(
            (s.ended_at for s in sessions if s.ended_at),
            default=None,
        )
        if last_end and (not record.last_leave or last_end > record.last_leave):
            record.last_leave = last_end
    db.add(record)


def _reconcile_all_players(db: DBSession) -> None:
    now = _utcnow()
    klei_ids = {r.klei_id for r in db.exec(select(PlayerRecord)).all()}
    klei_ids.update({s.klei_id for s in db.exec(select(PlayerSession)).all()})
    for klei_id in klei_ids:
        _reconcile_player_stats(db, klei_id, now)


def _parse_line_sort_key(line: str, file_index: int, line_index: int) -> tuple:
    m = _RE_LOG_TS.match(line.strip())
    if m:
        return (0, m.group(1), file_index, line_index)
    return (1, file_index, line_index, 0)


def _get_or_create_record(db: DBSession, klei_id: str) -> PlayerRecord:
    record = db.exec(select(PlayerRecord).where(PlayerRecord.klei_id == klei_id)).first()
    if record:
        return record
    now = _utcnow()
    record = PlayerRecord(
        klei_id=klei_id,
        first_seen=now,
        last_seen=now,
        join_count=0,
        session_count=0,
        total_playtime_seconds=0,
        is_online=False,
    )
    db.add(record)
    return record


def _file_ref_date(path: str):
    if os.path.exists(path):
        return datetime.fromtimestamp(os.path.getmtime(path)).date()
    return _utcnow().date()


class _ShardTimeCtx:
    def __init__(self, ref_date):
        self.ref_date = ref_date
        self.last_dt: Optional[datetime] = None

    def parse_line(self, line: str) -> datetime:
        m = _RE_LOG_TS.match(line.strip())
        if not m:
            return self.last_dt or datetime.combine(self.ref_date, datetime.min.time())
        parts = m.group(1).split(":")
        h, mi, s = int(parts[0]), int(parts[1]), int(parts[2])
        dt = datetime.combine(
            self.ref_date,
            datetime.min.time().replace(hour=h, minute=mi, second=s),
        )
        if self.last_dt and dt < self.last_dt:
            dt += timedelta(days=1)
            self.ref_date = dt.date()
        self.last_dt = dt
        return dt


def _open_session(
    db: DBSession,
    klei_id: str,
    name: Optional[str],
    ip: Optional[str],
    shard: str,
    event_at: Optional[datetime] = None,
) -> PlayerSession:
    existing = db.exec(
        select(PlayerSession).where(
            PlayerSession.klei_id == klei_id,
            PlayerSession.ended_at == None,  # noqa: E711
        )
    ).first()
    if existing:
        if name and not existing.name:
            existing.name = name
        if ip:
            existing.ip_address = ip
        db.add(existing)
        return existing

    now = event_at or _utcnow()
    record = _get_or_create_record(db, klei_id)
    if name:
        record.name = name
    if ip:
        record.last_ip = ip
        if not record.first_ip:
            record.first_ip = ip
    if not record.first_seen:
        record.first_seen = now
    record.last_seen = now
    record.last_join = now
    record.join_count = (record.join_count or 0) + 1
    record.session_count = (record.session_count or 0) + 1
    record.is_online = True
    db.add(record)

    session = PlayerSession(
        klei_id=klei_id,
        name=name or record.name,
        started_at=now,
        ip_address=ip,
        shard=shard,
    )
    db.add(session)
    return session


def _close_session(
    db: DBSession,
    klei_id: str,
    event_at: Optional[datetime] = None,
) -> Optional[PlayerSession]:
    session = db.exec(
        select(PlayerSession).where(
            PlayerSession.klei_id == klei_id,
            PlayerSession.ended_at == None,  # noqa: E711
        ).order_by(PlayerSession.started_at.desc())
    ).first()
    if not session:
        return None

    now = event_at or _utcnow()
    session.ended_at = now
    session.duration_seconds = max(0, int((now - session.started_at).total_seconds()))
    db.add(session)

    record = _get_or_create_record(db, klei_id)
    record.last_seen = now
    record.last_leave = now
    record.is_online = False
    db.add(record)
    return session


def _resolve_klei_id(
    name: str,
    name_to_klei: Dict[str, str],
    klei_to_name: Dict[str, str],
) -> Optional[str]:
    if not name:
        return None
    name = name.strip()
    if name in name_to_klei:
        return name_to_klei[name]
    lower_map = {k.lower(): v for k, v in name_to_klei.items()}
    return lower_map.get(name.lower())


def _process_server_log_line(
    db: DBSession,
    line: str,
    shard: str,
    name_to_klei: Dict[str, str],
    klei_to_name: Dict[str, str],
    pending_ip: Dict[str, str],
    steam_to_klei: Dict[str, str],
    event_at: Optional[datetime] = None,
) -> None:
    """server_log.txt: привязка ID/IP, закрытие по disconnect. Сессии — из chat или join/leave здесь."""
    if "ID_DST_SHARD_SILENT_DISCONNECT" in line:
        return

    m = RE_STEAM_DISCONNECT.search(line)
    if m:
        steam_id = m.group(1)
        klei_id = steam_to_klei.get(steam_id)
        if klei_id:
            _close_session(db, klei_id, event_at)
        return

    m = RE_JOIN_ANN.search(line)
    if m:
        player_name = m.group(1).strip()
        klei_id = _resolve_klei_id(player_name, name_to_klei, klei_to_name)
        if klei_id:
            name_to_klei[player_name] = klei_id
            klei_to_name[klei_id] = player_name
            _open_session(
                db, klei_id, player_name, pending_ip.get("last"), shard, event_at
            )
        return

    m = RE_LEAVE_ANN.search(line)
    if m:
        player_name = m.group(1).strip()
        klei_id = _resolve_klei_id(player_name, name_to_klei, klei_to_name)
        if klei_id:
            _close_session(db, klei_id, event_at)
        return

    m = RE_INCOMING.search(line) or RE_CONNECT.search(line)
    if m:
        pending_ip["last"] = m.group(1)
        return

    m = RE_VALIDATE.search(line)
    if m:
        pending_ip["klei"] = m.group(1)
        return

    m = RE_AUTH.search(line)
    if m:
        klei_id = m.group(1)
        name = (m.group(2) or "").strip() or None
        ip = pending_ip.pop("last", None)
        pending_ip.pop("klei", None)
        pending_ip["pending_steam_klei"] = klei_id
        if name:
            name_to_klei[name] = klei_id
            klei_to_name[klei_id] = name
        record = _get_or_create_record(db, klei_id)
        if name:
            record.name = name
        if ip:
            record.last_ip = ip
            if not record.first_ip:
                record.first_ip = ip
        record.last_seen = _utcnow()
        db.add(record)
        return

    m = RE_STEAM_AUTH.search(line)
    if m:
        steam_id = m.group(1)
        klei_id = pending_ip.pop("pending_steam_klei", None)
        if klei_id:
            steam_to_klei[steam_id] = klei_id
        return

    m = RE_USER_OWNERSHIP.search(line)
    if m:
        klei_id = m.group(1)
        record = _get_or_create_record(db, klei_id)
        record.last_seen = _utcnow()
        db.add(record)
        return

    m = RE_SPAWN.search(line)
    if m:
        player_name = m.group(1).strip()
        klei_id = _resolve_klei_id(player_name, name_to_klei, klei_to_name)
        if klei_id:
            name_to_klei[player_name] = klei_id
            klei_to_name[klei_id] = player_name
            record = _get_or_create_record(db, klei_id)
            record.name = player_name
            record.last_seen = _utcnow()
            db.add(record)
        return

    m = RE_SAVE.search(line)
    if m:
        klei_id = m.group(1)
        record = _get_or_create_record(db, klei_id)
        record.last_seen = _utcnow()
        db.add(record)
        return

    m = RE_CHAT.search(line)
    if m:
        klei_id = m.group(1)
        name = m.group(2).strip()
        name_to_klei[name] = klei_id
        klei_to_name[klei_id] = name
        record = _get_or_create_record(db, klei_id)
        record.name = name
        record.last_seen = _utcnow()
        db.add(record)


def _process_chat_log_line(
    db: DBSession,
    line: str,
    shard: str,
    name_to_klei: Dict[str, str],
    klei_to_name: Dict[str, str],
    pending_ip: Dict[str, str],
    event_at: Optional[datetime] = None,
) -> None:
    """server_chat_log.txt: [Join/Leave Announcement] — границы сессий."""
    m = RE_JOIN_ANN.search(line)
    if m:
        player_name = m.group(1).strip()
        klei_id = _resolve_klei_id(player_name, name_to_klei, klei_to_name)
        if klei_id:
            name_to_klei[player_name] = klei_id
            klei_to_name[klei_id] = player_name
            _open_session(
                db, klei_id, player_name, pending_ip.get("last"), shard, event_at
            )
        return

    m = RE_LEAVE_ANN.search(line)
    if m:
        player_name = m.group(1).strip()
        klei_id = _resolve_klei_id(player_name, name_to_klei, klei_to_name)
        if klei_id:
            _close_session(db, klei_id, event_at)


def _process_log_line(
    db: DBSession,
    line: str,
    shard: str,
    filename: str,
    name_to_klei: Dict[str, str],
    klei_to_name: Dict[str, str],
    pending_ip: Dict[str, str],
    steam_to_klei: Dict[str, str],
    event_at: Optional[datetime] = None,
) -> None:
    if filename == "server_chat_log.txt":
        _process_chat_log_line(
            db, line, shard, name_to_klei, klei_to_name, pending_ip, event_at
        )
    else:
        _process_server_log_line(
            db,
            line,
            shard,
            name_to_klei,
            klei_to_name,
            pending_ip,
            steam_to_klei,
            event_at,
        )


def sync_players_from_logs() -> dict:
    engine = get_engine()
    with DBSession(engine) as db:
        _ensure_sync_v2(db)
        _ensure_sync_v5_reparse(db)
        _ensure_sync_v6_log_timestamps(db)
        _ensure_sync_v4(db)

        name_to_klei: Dict[str, str] = {}
        klei_to_name: Dict[str, str] = {}
        pending_ip: Dict[str, str] = {}
        steam_to_klei: Dict[str, str] = _load_steam_map(db)
        shard_time: Dict[str, _ShardTimeCtx] = {}
        processed = 0

        for record in db.exec(select(PlayerRecord)).all():
            if record.name and record.klei_id:
                name_to_klei[record.name] = record.klei_id
                klei_to_name[record.klei_id] = record.name

        file_order = list(_SERVER_LOG_FILES) + list(_CHAT_LOG_FILES)
        for shard, filename in file_order:
            path = _shard_log_path(shard, filename)
            ctx = shard_time.get(shard)
            if ctx is None:
                ctx = _ShardTimeCtx(_file_ref_date(path))
                shard_time[shard] = ctx
            for line in _read_new_log_lines(db, shard, filename):
                event_at = ctx.parse_line(line)
                _process_log_line(
                    db,
                    line,
                    shard,
                    filename,
                    name_to_klei,
                    klei_to_name,
                    pending_ip,
                    steam_to_klei,
                    event_at,
                )
                processed += 1

        _reconcile_all_players(db)
        _save_steam_map(db, steam_to_klei)
        db.commit()

        online_count = len(
            db.exec(
                select(PlayerSession).where(PlayerSession.ended_at == None)  # noqa: E711
            ).all()
        )

    invalidate_players_cache()
    return {"synced_lines": processed, "online_count": online_count}


def _fmt_iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _human_duration(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м"
    if m:
        return f"{m}м {s}с"
    return f"{s}с"


def _build_daily_stats(
    sessions: List[PlayerSession],
    days: int = 30,
) -> List[dict]:
    today = _utcnow().date()
    start = today - timedelta(days=days - 1)
    buckets: Dict[str, dict] = {}

    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        buckets[d] = {
            "date": d,
            "playtime_seconds": 0,
            "session_count": 0,
            "unique_players": set(),
        }

    for session in sessions:
        if not session.started_at:
            continue
        end = session.ended_at or _utcnow()
        if end <= session.started_at:
            continue
        counted_days: set = set()
        cursor = session.started_at
        while cursor.date() <= end.date():
            day_key = cursor.date().isoformat()
            if day_key in buckets:
                day_start = datetime.combine(cursor.date(), datetime.min.time())
                day_end = datetime.combine(cursor.date(), datetime.max.time())
                seg_start = max(session.started_at, day_start)
                seg_end = min(end, day_end)
                seconds = max(0, int((seg_end - seg_start).total_seconds()))
                if seconds > 0:
                    buckets[day_key]["playtime_seconds"] += seconds
                    if day_key not in counted_days:
                        buckets[day_key]["session_count"] += 1
                        buckets[day_key]["unique_players"].add(session.klei_id)
                        counted_days.add(day_key)
            cursor = datetime.combine(cursor.date(), datetime.min.time()) + timedelta(days=1)

    result = []
    for d in sorted(buckets.keys()):
        b = buckets[d]
        result.append({
            "date": b["date"],
            "playtime_seconds": b["playtime_seconds"],
            "session_count": b["session_count"],
            "unique_players": len(b["unique_players"]),
        })
    return result


def _build_dashboard(db: DBSession, sessions: List[PlayerSession]) -> dict:
    records = db.exec(select(PlayerRecord)).all()
    now = _utcnow()
    today = now.date()
    today_start = datetime.combine(today, datetime.min.time())

    open_sessions = [s for s in sessions if s.ended_at is None]
    ended_today = [
        s for s in sessions
        if s.ended_at and s.ended_at >= today_start
    ]
    daily = _build_daily_stats(sessions, 14)
    today_key = today.isoformat()
    playtime_today = next(
        (d["playtime_seconds"] for d in daily if d["date"] == today_key),
        0,
    )

    total_playtime = sum(_session_duration(s, now) for s in sessions)
    top_players = sorted(
        records,
        key=lambda r: r.total_playtime_seconds or 0,
        reverse=True,
    )[:5]

    return {
        "unique_players": len(records),
        "online_count": len(open_sessions),
        "total_playtime_seconds": total_playtime,
        "total_sessions": len(sessions),
        "sessions_today": len(ended_today) + len(open_sessions),
        "playtime_today_seconds": playtime_today,
        "daily_activity": daily,
        "top_players": [
            {
                "klei_id": r.klei_id,
                "name": r.name or r.klei_id,
                "total_playtime_seconds": r.total_playtime_seconds or 0,
                "total_playtime_human": _human_duration(r.total_playtime_seconds or 0),
                "session_count": r.session_count or 0,
            }
            for r in top_players
            if (r.total_playtime_seconds or 0) > 0 or (r.session_count or 0) > 0
        ],
    }


def _session_to_dict(session: PlayerSession) -> dict:
    return {
        "id": session.id,
        "klei_id": session.klei_id,
        "name": session.name,
        "started_at": _fmt_iso(session.started_at),
        "ended_at": _fmt_iso(session.ended_at),
        "duration_seconds": _session_duration(session),
        "duration_human": _human_duration(_session_duration(session)),
        "ip_address": session.ip_address,
        "shard": session.shard,
        "active": session.ended_at is None,
    }


def get_player_detail(klei_id: str) -> Optional[dict]:
    sync_players_from_logs()
    engine = get_engine()
    with DBSession(engine) as db:
        record = db.exec(
            select(PlayerRecord).where(PlayerRecord.klei_id == klei_id)
        ).first()
        if not record:
            return None

        sessions = db.exec(
            select(PlayerSession)
            .where(PlayerSession.klei_id == klei_id)
            .order_by(PlayerSession.started_at.desc())
            .limit(100)
        ).all()

        all_sessions = db.exec(
            select(PlayerSession).where(PlayerSession.klei_id == klei_id)
        ).all()
        daily = _build_daily_stats(all_sessions, 30)

        open_session = next((s for s in sessions if s.ended_at is None), None)
        roles = get_player_roles(klei_id)

        return {
            "player": {
                "klei_id": record.klei_id,
                "name": record.name or record.klei_id,
                "first_seen": _fmt_iso(record.first_seen),
                "last_seen": _fmt_iso(record.last_seen),
                "last_join": _fmt_iso(record.last_join),
                "last_leave": _fmt_iso(record.last_leave),
                "first_ip": record.first_ip,
                "last_ip": record.last_ip,
                "join_count": record.join_count or 0,
                "session_count": record.session_count or 0,
                "total_playtime_seconds": record.total_playtime_seconds or 0,
                "total_playtime_human": _human_duration(record.total_playtime_seconds or 0),
                "online": record.is_online,
                "current_session": _session_to_dict(open_session) if open_session else None,
                "roles": roles,
            },
            "sessions": [_session_to_dict(s) for s in sessions],
            "daily": daily,
        }


def get_players_overview(force_sync: bool = False) -> dict:
    now = time.time()
    if (
        not force_sync
        and _overview_cache["data"]
        and now - _overview_cache["at"] < _CACHE_TTL
    ):
        return _overview_cache["data"]

    sync_players_from_logs()

    lists = read_all_lists()
    engine = get_engine()
    with DBSession(engine) as db:
        records = db.exec(
            select(PlayerRecord).order_by(PlayerRecord.last_seen.desc())
        ).all()
        all_sessions = db.exec(select(PlayerSession)).all()
        open_sessions = {
            s.klei_id: s
            for s in all_sessions
            if s.ended_at is None
        }

        dashboard = _build_dashboard(db, all_sessions)
        players = []
        seen_ids = set()

        for rec in records:
            seen_ids.add(rec.klei_id)
            open_sess = open_sessions.get(rec.klei_id)
            player_sessions = [s for s in all_sessions if s.klei_id == rec.klei_id]
            daily = _build_daily_stats(player_sessions, 14)
            players.append({
                "klei_id": rec.klei_id,
                "name": rec.name or rec.klei_id,
                "first_seen": _fmt_iso(rec.first_seen),
                "last_seen": _fmt_iso(rec.last_seen),
                "last_join": _fmt_iso(rec.last_join),
                "last_leave": _fmt_iso(rec.last_leave),
                "first_ip": rec.first_ip,
                "last_ip": rec.last_ip,
                "join_count": rec.join_count or 0,
                "session_count": rec.session_count or 0,
                "total_playtime_seconds": rec.total_playtime_seconds or 0,
                "total_playtime_human": _human_duration(rec.total_playtime_seconds or 0),
                "online": rec.klei_id in open_sessions,
                "current_session_started": _fmt_iso(open_sess.started_at) if open_sess else None,
                "shard": open_sess.shard if open_sess else None,
                "daily_playtime": daily,
                "roles": get_player_roles(rec.klei_id),
            })

        for kid, sess in open_sessions.items():
            if kid not in seen_ids:
                players.insert(0, {
                    "klei_id": kid,
                    "name": sess.name or kid,
                    "first_seen": _fmt_iso(sess.started_at),
                    "last_seen": _fmt_iso(sess.started_at),
                    "last_join": _fmt_iso(sess.started_at),
                    "last_leave": None,
                    "first_ip": sess.ip_address,
                    "last_ip": sess.ip_address,
                    "join_count": 1,
                    "session_count": 1,
                    "total_playtime_seconds": 0,
                    "total_playtime_human": "0с",
                    "online": True,
                    "current_session_started": _fmt_iso(sess.started_at),
                    "shard": sess.shard,
                    "daily_playtime": [],
                    "roles": get_player_roles(kid),
                })
                seen_ids.add(kid)

        for kid in lists["admin"] + lists["block"] + lists["whitelist"]:
            if kid not in seen_ids:
                players.append({
                    "klei_id": kid,
                    "name": kid,
                    "first_seen": None,
                    "last_seen": None,
                    "last_join": None,
                    "last_leave": None,
                    "first_ip": None,
                    "last_ip": None,
                    "join_count": 0,
                    "session_count": 0,
                    "total_playtime_seconds": 0,
                    "total_playtime_human": "0с",
                    "online": False,
                    "current_session_started": None,
                    "shard": None,
                    "daily_playtime": [],
                    "roles": get_player_roles(kid),
                })

    online = [
        {
            "klei_id": kid,
            "name": (open_sessions[kid].name if kid in open_sessions else None)
                or next((p["name"] for p in players if p["klei_id"] == kid), kid),
            "shard": open_sessions[kid].shard if kid in open_sessions else "Master",
            "since": _fmt_iso(open_sessions[kid].started_at) if kid in open_sessions else None,
            "ip_address": open_sessions[kid].ip_address if kid in open_sessions else None,
            "roles": get_player_roles(kid),
        }
        for kid in open_sessions
    ]

    log_master = os.path.exists(_shard_log_path("Master"))
    result = {
        "dashboard": dashboard,
        "online": online,
        "online_count": len(online),
        "players": players,
        "lists": lists,
        "log_available": log_master,
        "log_path": _shard_log_path("Master"),
        "note": (
            "Учёт по логам DST: сессия открывается по [Join Announcement], "
            "закрывается по [Leave Announcement] или SendUserDisconnect. "
            "Время считается по сессиям в базе панели (включая текущую)."
        ),
    }
    _overview_cache["data"] = result
    _overview_cache["at"] = now
    return result
