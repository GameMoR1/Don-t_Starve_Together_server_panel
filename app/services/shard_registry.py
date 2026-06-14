"""
Учёт запущенных шардов: сохранённый PID + обнаружение живых процессов DST.
"""
from __future__ import annotations

import errno
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psutil

from app.config.config_reader import normalize_shard

_RE_DST_CMD = re.compile(r"dontstarve_dedicated_server", re.IGNORECASE)
_RE_SHARD_MASTER = re.compile(r"-shard\s+Master\b", re.IGNORECASE)
_RE_SHARD_CAVES = re.compile(r"-shard\s+Caves\b", re.IGNORECASE)

REGISTRY_PATH = "/var/lib/dst-panel/shard-pids.json"

_OFFLINE_STATUS: Dict[str, Any] = {
    "running": False,
    "pid": None,
    "pids": [],
    "uptime": 0,
    "source": None,
    "systemd": False,
    "external": False,
    "confirmed": True,
    "port_open": False,
}


def _ensure_dir() -> None:
    os.makedirs(os.path.dirname(REGISTRY_PATH), exist_ok=True)


def _empty_registry() -> dict:
    return {"Master": None, "Caves": None}


def load_registry() -> dict:
    _ensure_dir()
    if not os.path.isfile(REGISTRY_PATH):
        return _empty_registry()
    try:
        with open(REGISTRY_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return _empty_registry()
    for shard in ("Master", "Caves"):
        data.setdefault(shard, None)
    return data


def save_registry(data: dict) -> None:
    _ensure_dir()
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(REGISTRY_PATH),
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp_path, REGISTRY_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        os.chmod(REGISTRY_PATH, 0o600)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.path.isdir(f"/proc/{pid}"):
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError as exc:
        if exc.errno == errno.EPERM:
            return True
        return False


def discover_shard_pids() -> Dict[str, List[int]]:
    """Живые процессы DST по командной строке (в т.ч. после рестарта панели)."""
    found: Dict[str, List[int]] = {"Master": [], "Caves": []}
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
            if not cmdline:
                continue
            cmd = " ".join(cmdline)
            if not _RE_DST_CMD.search(cmd):
                continue
            pid = int(proc.info["pid"])
            if _RE_SHARD_MASTER.search(cmd):
                found["Master"].append(pid)
            elif _RE_SHARD_CAVES.search(cmd):
                found["Caves"].append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError, ValueError):
            continue
    return found


def _pick_canonical_pid(pids: List[int]) -> Optional[int]:
    if not pids:
        return None
    if len(pids) == 1:
        return pids[0]
    best_pid = pids[0]
    best_time = None
    for pid in pids:
        try:
            create_time = psutil.Process(pid).create_time()
            if best_time is None or create_time < best_time:
                best_time = create_time
                best_pid = pid
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return best_pid


def get_record(shard: str) -> Optional[dict]:
    shard = normalize_shard(shard)
    record = load_registry().get(shard)
    return record if isinstance(record, dict) else None


def set_pid(shard: str, pid: int) -> dict:
    shard = normalize_shard(shard)
    data = load_registry()
    record = {
        "pid": int(pid),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    data[shard] = record
    save_registry(data)
    return record


def clear_shard(shard: str) -> None:
    shard = normalize_shard(shard)
    data = load_registry()
    data[shard] = None
    save_registry(data)


def _uptime_for_pid(pid: int) -> int:
    try:
        proc = psutil.Process(pid)
        return max(
            0,
            int(datetime.now(timezone.utc).timestamp() - proc.create_time()),
        )
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0


def status_for_shard(shard: str, *, clear_if_dead: bool = True) -> dict:
    shard = normalize_shard(shard)
    record = get_record(shard)
    pid = record.get("pid") if record else None
    source = "registry"

    if not pid or not pid_alive(pid):
        discovered = discover_shard_pids().get(shard, [])
        pid = _pick_canonical_pid(discovered)
        if pid:
            set_pid(shard, pid)
            record = get_record(shard)
            source = "discovered"
        elif clear_if_dead and record:
            clear_shard(shard)
            return dict(_OFFLINE_STATUS)
        else:
            return dict(_OFFLINE_STATUS)

    return {
        "running": True,
        "pid": pid,
        "pids": [pid],
        "uptime": _uptime_for_pid(pid),
        "source": source,
        "systemd": False,
        "external": source == "discovered",
        "confirmed": True,
        "port_open": False,
        "started_at": record.get("started_at") if record else None,
    }


def reconcile_registry() -> dict:
    """Синхронизировать реестр с реально работающими шардами (после рестарта панели)."""
    discovered = discover_shard_pids()
    updated: Dict[str, int] = {}
    cleared: List[str] = []

    for shard in ("Master", "Caves"):
        pids = discovered.get(shard, [])
        canonical = _pick_canonical_pid(pids)
        record = get_record(shard)
        reg_pid = record.get("pid") if record else None

        if canonical:
            if reg_pid != canonical:
                set_pid(shard, canonical)
                updated[shard] = canonical
        elif reg_pid and not pid_alive(reg_pid):
            clear_shard(shard)
            cleared.append(shard)

    return {
        "updated": updated,
        "cleared": cleared,
        "discovered": discovered,
    }


def registry_snapshot() -> dict:
    data = load_registry()
    return {
        "path": REGISTRY_PATH,
        "records": data,
        "master": status_for_shard("Master", clear_if_dead=False),
        "caves": status_for_shard("Caves", clear_if_dead=False),
    }
