import os
import re
import signal
import time
import shutil
import asyncio
import subprocess
from datetime import datetime, timezone
from typing import List, Optional

import psutil

from app.services import shard_registry
from app.services.shard_registry import pid_alive
from app.config.config_reader import (
    normalize_shard,
    read_cluster_ini,
    read_cluster_token,
    read_shard_ini,
    DST_DIR,
    CLUSTER_DIR,
    refresh_dst_paths,
    get_dst_dir,
    get_cluster_dir,
    ensure_shard_link_config,
    get_cluster_binding_status,
    bind_master_to_cluster,
    bind_caves_to_master,
    apply_cluster_bindings,
    validate_cluster_config,
    cluster_shards_enabled,
)
BACKUP_DIR = "/var/lib/dst-panel/backups"
PANEL_LOG_DIR = "/var/lib/dst-panel/shard-logs"


def _steamcmd_dir() -> str:
    return os.path.join(get_dst_dir(), "steamcmd")


def _mods_dir() -> str:
    return os.path.join(get_dst_dir(), "mods")


def _dst_binary() -> str:
    return os.path.join(_bin_dir(), "dontstarve_dedicated_server_nullrenderer")

_RE_SERVER_START = re.compile(
    r"Starting dedicated server|Command Line Arguments|DEDICATED SERVER",
    re.IGNORECASE,
)

_PANEL_PROCESSES: dict = {}
START_VERIFY_SECONDS = 8
CAVES_LINK_VERIFY_SECONDS = 30
MASTER_WARMUP_SECONDS = 10

_RE_MASTER_SHARD_DISABLED = re.compile(
    r"Shard server mode disabled",
    re.IGNORECASE,
)
_RE_CAVES_NOT_CONNECTED = re.compile(
    r"no available shard \[Caves\] connected",
    re.IGNORECASE,
)
_RE_CAVES_LINKED = re.compile(
    r"Registering slave|"
    r"\[Shard\].*(?:Caves|slave).*(?:connect|regist|linked|available|migration)|"
    r"Shard.*Caves.*(?:connected|registered)|"
    r"Slave.*(?:ready|connected)",
    re.IGNORECASE,
)
_RE_WORLD_READY = re.compile(
    r"World is now ready|Online Server Started|Begin Session|Server is ready|"
    r"Dedicated server started|Server has started|Simulating world",
    re.IGNORECASE,
)
_RE_WORLD_GENERATED = re.compile(
    r"Generating world|World generated|Creating world|WorldSim|"
    r"Generating new world|New world generated",
    re.IGNORECASE,
)
_RE_PANEL_LAUNCH = re.compile(
    r"=== Запуск\s+(Master|Caves)\s+",
    re.IGNORECASE,
)

_REGEN_STATE: dict = {
    "active": False,
    "step": "idle",
    "percent": 0,
    "message": "",
    "error": None,
    "started_at": None,
    "finished_at": None,
    "details": {},
}
_REGEN_LOCK = asyncio.Lock()
_regen_task: Optional[asyncio.Task] = None
_cancel_event = asyncio.Event()


def _get_dst_env() -> dict:
    env = os.environ.copy()
    bin_dir = _bin_dir()
    env.update({
        "LD_LIBRARY_PATH": f"{bin_dir}/lib32:{bin_dir}/lib64",
        "STEAMPIPE_PATH": f"{_steamcmd_dir()}/linux32",
        "PERSISTENT_STORAGE_ROOT": get_dst_dir(),
    })
    return env


def _dst_paths_runtime() -> dict:
    paths = refresh_dst_paths()
    dst_dir = paths["dst_dir"]
    cluster_dir = paths["cluster_dir"]
    return {
        "dst_dir": dst_dir,
        "cluster_dir": cluster_dir,
        "bin_dir": os.path.join(dst_dir, "bin"),
        "master_dir": os.path.join(cluster_dir, "Master"),
        "caves_dir": os.path.join(cluster_dir, "Caves"),
    }


def _shard_dir(shard: str) -> str:
    shard = normalize_shard(shard)
    paths = _dst_paths_runtime()
    return paths["master_dir"] if shard == "Master" else paths["caves_dir"]


def _bin_dir() -> str:
    return _dst_paths_runtime()["bin_dir"]


def _ensure_panel_log_dir():
    os.makedirs(PANEL_LOG_DIR, exist_ok=True)


def _launch_log_path(shard: str) -> str:
    return os.path.join(PANEL_LOG_DIR, f"{normalize_shard(shard).lower()}_launch.log")


def _read_log_file(path: str, lines: int) -> List[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-lines:]]
    except Exception:
        return []


def _discover_log_files(shard: str) -> List[dict]:
    shard = normalize_shard(shard)
    shard_dir = _shard_dir(shard)
    discovered = []

    def add(label: str, path: str):
        if not os.path.isfile(path):
            return
        try:
            discovered.append({
                "label": label,
                "path": path,
                "mtime": os.path.getmtime(path),
                "size": os.path.getsize(path),
            })
        except OSError:
            pass

    add("server_log.txt", os.path.join(shard_dir, "server_log.txt"))
    add("server_chat_log.txt", os.path.join(shard_dir, "server_chat_log.txt"))
    add("client_log.txt", os.path.join(shard_dir, "client_log.txt"))
    add("panel_launch.log", _launch_log_path(shard))
    add("bin/server_log.txt", os.path.join(_bin_dir(), "server_log.txt"))

    backup_dir = os.path.join(shard_dir, "backup")
    if os.path.isdir(backup_dir):
        for name in sorted(os.listdir(backup_dir), reverse=True):
            lower = name.lower()
            if "log" in lower and (lower.endswith(".txt") or lower.endswith(".log")):
                add(f"backup/{name}", os.path.join(backup_dir, name))

    home = os.path.expanduser("~")
    klei_root = os.path.join(home, ".klei", "DoNotStarveTogether")
    if os.path.isdir(klei_root):
        for root, _, files in os.walk(klei_root):
            if normalize_shard(shard) not in root:
                continue
            if "server_log.txt" in files:
                rel = os.path.relpath(root, klei_root)
                add(f"klei/{rel}/server_log.txt", os.path.join(root, "server_log.txt"))

    discovered.sort(key=lambda item: item["mtime"], reverse=True)
    return discovered


def _collect_log_lines(shard: str, lines: int = 100) -> dict:
    shard = normalize_shard(shard)
    shard_dir = _shard_dir(shard)
    expected = os.path.join(shard_dir, "server_log.txt")
    discovered = _discover_log_files(shard)
    sources = [
        {"label": d["label"], "path": d["path"], "size": d["size"]}
        for d in discovered
    ]

    if not discovered:
        return {
            "lines": [],
            "found": False,
            "empty": True,
            "path": expected,
            "source": "server_log.txt",
            "message": (
                f"Лог не найден. Ожидаемый путь: {expected}. "
                "Запустите шард — после старта файл должен появиться."
            ),
            "sources": [],
        }

    primary = next(
        (d for d in discovered if d["label"] == "server_log.txt"),
        discovered[0],
    )
    collected: List[str] = []
    main_lines = _read_log_file(primary["path"], lines)
    if main_lines:
        collected.extend(main_lines)
    else:
        for alt in discovered:
            if alt["path"] == primary["path"]:
                continue
            alt_lines = _read_log_file(alt["path"], min(lines, 80))
            if not alt_lines:
                continue
            collected.append(f"--- {alt['label']}: {alt['path']} ---")
            collected.extend(alt_lines)
            if len(collected) >= 10:
                break

    if not collected:
        return {
            "lines": [],
            "found": True,
            "empty": True,
            "path": primary["path"],
            "source": primary["label"],
            "message": (
                f"Файл {primary['path']} пуст. "
                "Если шард падает сразу — смотрите panel_launch.log ниже после повторного запуска."
            ),
            "sources": sources,
        }

    return {
        "lines": collected[-lines:],
        "found": True,
        "empty": False,
        "path": primary["path"],
        "source": primary["label"],
        "message": "",
        "sources": sources,
    }


def _get_log_tail(shard: str, lines: int = 20) -> List[str]:
    return _collect_log_lines(shard, lines).get("lines", [])


def get_shard_runtime_health(
    master_status: Optional[dict] = None,
    caves_status: Optional[dict] = None,
) -> dict:
    """Проверяет по логам и конфигу, подключены ли Caves к Master."""
    master_status = master_status or get_shard_status("Master")
    caves_status = caves_status or get_shard_status("Caves")
    binding = get_cluster_binding_status()
    master_running = master_status["running"]
    caves_running = caves_status["running"]

    master_log = "\n".join(_get_session_log_tail("Master", 500))
    caves_log = "\n".join(_get_session_log_tail("Caves", 250))

    shard_mode_disabled = bool(
        master_running and _RE_MASTER_SHARD_DISABLED.search(master_log)
    )
    config_shards_enabled = binding["cluster"]["shards_enabled"]
    binding_ok = binding["master"]["synced"] and binding["caves"]["synced"]

    caves_not_connected_msg = bool(
        master_running and _RE_CAVES_NOT_CONNECTED.search(master_log)
    )
    caves_linked_signal = bool(_RE_CAVES_LINKED.search(master_log))

    recent_master = master_log[-12000:] if len(master_log) > 12000 else master_log
    recent_no_caves_error = (
        caves_running
        and master_running
        and not _RE_CAVES_NOT_CONNECTED.search(recent_master)
    )

    caves_linked = False
    if caves_running and master_running:
        if caves_linked_signal or recent_no_caves_error:
            caves_linked = True
        elif binding_ok and "Online Server Started" in caves_log:
            caves_linked = True

    needs_master_restart = (
        master_running
        and config_shards_enabled
        and binding_ok
        and shard_mode_disabled
        and not caves_linked
    )

    messages = []
    if not config_shards_enabled:
        messages.append("shard_enabled=false в cluster.ini — пещеры отключены.")
    if needs_master_restart:
        messages.append(
            "Master запущен без режима шардов. Остановите кластер и запустите снова "
            "(после исправления shard_enabled=true)."
        )
    elif shard_mode_disabled and master_running and not caves_linked:
        messages.append("В логе Master: Shard server mode disabled.")
    if master_running and caves_running and not caves_linked:
        messages.append("Caves запущен, но в логе Master нет подключения пещер.")
    elif master_running and not caves_running and caves_not_connected_msg:
        messages.append("Master ждёт Caves — запустите шард Caves.")
    elif master_running and caves_running and caves_linked:
        messages.append("Caves подключён к Master.")

    return {
        "master_running": master_running,
        "caves_running": caves_running,
        "config_shards_enabled": config_shards_enabled,
        "binding_ok": binding_ok,
        "master_shard_mode_disabled": shard_mode_disabled,
        "master_shard_mode_ok": master_running and not shard_mode_disabled and config_shards_enabled,
        "caves_linked": caves_linked,
        "caves_not_connected": caves_not_connected_msg and not caves_linked,
        "needs_master_restart": needs_master_restart,
        "messages": messages,
        "binding": binding,
    }


def _log_lines_since_last_start(lines: List[str]) -> List[str]:
    if not lines:
        return []
    last = 0
    for idx, line in enumerate(lines):
        if _RE_SERVER_START.search(line):
            last = idx
    return lines[last:]


def _get_session_log_tail(shard: str, lines: int = 500) -> List[str]:
    return _log_lines_since_last_start(_get_log_tail(shard, lines))


def is_shard_running(shard: str) -> bool:
    return bool(get_shard_status(shard).get("running"))


def _cleanup_panel_process(shard: str) -> None:
    info = _PANEL_PROCESSES.get(shard)
    if not info:
        return
    proc = info.get("process")
    if proc is None or proc.returncode is not None:
        _PANEL_PROCESSES.pop(shard, None)


def _libcurl_gnutls_ok() -> bool:
    candidates = [
        "/usr/lib/i386-linux-gnu/libcurl-gnutls.so.4",
        "/lib/i386-linux-gnu/libcurl-gnutls.so.4",
    ]
    if any(os.path.isfile(p) or os.path.islink(p) for p in candidates):
        return True
    if not os.path.isfile(_dst_binary()):
        return True
    try:
        result = subprocess.run(
            ["ldd", _dst_binary()],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            if "libcurl-gnutls" in line:
                return "not found" not in line
    except Exception:
        pass
    return False


def libcurl_gnutls_ok() -> bool:
    return _libcurl_gnutls_ok()


def get_shard_prerequisites(shard: str) -> dict:
    shard = normalize_shard(shard)
    master_status = get_shard_status("Master")
    caves_status = get_shard_status("Caves")
    cluster = read_cluster_ini()
    offline = cluster.get("NETWORK.offline_cluster", "false").lower() == "true"
    shards_enabled = cluster_shards_enabled(cluster)
    token = read_cluster_token()

    checks = [
        {
            "id": "binary",
            "label": "Файлы DST-сервера",
            "ok": os.path.isfile(_dst_binary()),
            "required": True,
            "hint": "Установите DST через «Установить DST» или install.sh",
        },
        {
            "id": "libcurl_gnutls",
            "label": "libcurl-gnutls (32-bit)",
            "ok": libcurl_gnutls_ok(),
            "required": True,
            "hint": "На сервере: sudo apt install libcurl3-gnutls:i386 && sudo bash scripts/install.sh",
        },
        {
            "id": "cluster_ini",
            "label": "cluster.ini",
            "ok": os.path.isfile(f"{get_cluster_dir()}/cluster.ini"),
            "required": True,
            "hint": "Создайте конфиг на странице «Запуск»",
        },
        {
            "id": "shard_ini",
            "label": f"{shard}/server.ini",
            "ok": os.path.isfile(f"{_shard_dir(shard)}/server.ini"),
            "required": True,
            "hint": f"Настройте шард {shard} в разделе «Конфиг»",
        },
        {
            "id": "token",
            "label": "Cluster Token",
            "ok": bool(token) or offline,
            "required": not offline,
            "hint": "Сохраните токен Klei (Конфиг → Токен) или включите офлайн-режим",
        },
    ]

    if shard == "Caves" and shards_enabled:
        master_up = master_status["running"]
        checks.append({
            "id": "master_running",
            "label": "Master запущен",
            "ok": master_up,
            "required": True,
            "hint": "Сначала запустите шард Master",
        })
        binding = get_cluster_binding_status()
        checks.append({
            "id": "caves_link",
            "label": "Caves привязан к Master",
            "ok": binding["caves"]["synced"],
            "required": True,
            "hint": (
                "Конфиг → Caves → «Привязать к Master» "
                "(нужен SHARD.master_ip и is_master=false, иначе в браузере два сервера)"
            ),
        })

    if shards_enabled and shard == "Master":
        binding = get_cluster_binding_status()
        checks.append({
            "id": "shards_enabled_ini",
            "label": "shard_enabled в cluster.ini",
            "ok": shards_enabled,
            "required": True,
            "hint": "Конфиг → Кластер: включите «Шарды (пещеры)» (shard_enabled=true)",
        })
        checks.append({
            "id": "master_link",
            "label": "Master ↔ cluster.ini",
            "ok": binding["master"]["synced"],
            "required": False,
            "hint": "Исправится автоматически при запуске",
        })

    if shard == "Caves" and shards_enabled:
        health = get_shard_runtime_health(master_status, caves_status)
        if caves_status["running"]:
            if health["caves_linked"]:
                checks.append({
                    "id": "master_shard_mode",
                    "label": "Master в режиме шардов",
                    "ok": True,
                    "required": False,
                    "hint": "Caves подключён к Master",
                })
            elif health["master_running"] and health["needs_master_restart"]:
                checks.append({
                    "id": "master_shard_mode",
                    "label": "Master в режиме шардов",
                    "ok": False,
                    "required": True,
                    "hint": (
                        "Перезапустите кластер: Master стартовал с shard_enabled=false. "
                        "Панель уже исправила cluster.ini — нужен рестарт Master."
                    ),
                })
            elif health["master_running"]:
                checks.append({
                    "id": "master_shard_mode",
                    "label": "Master в режиме шардов",
                    "ok": health["master_shard_mode_ok"],
                    "required": True,
                    "hint": "Перезапустите кластер после включения shard_enabled в cluster.ini",
                })
        elif health["master_running"] and health["needs_master_restart"]:
            checks.append({
                "id": "master_shard_mode",
                "label": "Master в режиме шардов",
                "ok": False,
                "required": True,
                "hint": (
                    "Перезапустите кластер: Master стартовал с shard_enabled=false. "
                    "Панель уже исправила cluster.ini — нужен рестарт Master."
                ),
            })
        elif health["master_running"]:
            checks.append({
                "id": "master_shard_mode",
                "label": "Master в режиме шардов",
                "ok": health["master_shard_mode_ok"],
                "required": True,
                "hint": "Перезапустите кластер после включения shard_enabled в cluster.ini",
            })

    required = [c for c in checks if c["required"]]
    return {
        "shard": shard,
        "ready": all(c["ok"] for c in required),
        "checks": checks,
    }


def get_shard_status(shard: str = "Master") -> dict:
    shard = normalize_shard(shard)
    refresh_dst_paths()
    _cleanup_panel_process(shard)
    return shard_registry.status_for_shard(shard)


async def _verify_shard_started(shard: str, expected_pid: int) -> dict:
    for _ in range(START_VERIFY_SECONDS):
        await asyncio.sleep(1)
        if expected_pid and pid_alive(expected_pid):
            return {"ok": True, "pid": expected_pid}

    log_data = _collect_log_lines(shard, 40)
    log_tail = log_data.get("lines", [])
    err = "Процесс завершился сразу после запуска"
    if log_tail:
        err = f"{err}. См. лог ниже"
    return {"ok": False, "error": err, "log_tail": log_tail, "log_path": log_data.get("path")}


async def _verify_caves_linked_to_master(timeout_seconds: Optional[int] = None) -> dict:
    timeout = timeout_seconds if timeout_seconds is not None else CAVES_LINK_VERIFY_SECONDS
    try:
        async def _poll():
            for _ in range(timeout):
                if _cancel_event.is_set():
                    return {"ok": False, "error": "Cancelled", "_cancelled": True}
                await asyncio.sleep(1)
                health = get_shard_runtime_health()
                if health["caves_linked"]:
                    return {"ok": True, "health": health}
                if not health["caves_running"]:
                    return {
                        "ok": False,
                        "error": "Процесс Caves завершился до подключения к Master",
                        "health": health,
                    }
            health = get_shard_runtime_health()
            err = f"Caves запущен, но не подключился к Master за {timeout} секунд."
            if health["needs_master_restart"]:
                err = (
                    "Caves не может подключиться: Master работает без режима шардов. "
                    "Остановите Master → запустите Master → затем Caves."
                )
            elif not health["binding_ok"]:
                err = "Конфиг шардов не синхронизирован. Конфиг → Caves → «Привязать к Master»."
            return {"ok": False, "error": err, "health": health}
        return await asyncio.wait_for(_poll(), timeout=timeout + 5)
    except asyncio.TimeoutError:
        health = get_shard_runtime_health()
        return {
            "ok": False,
            "error": f"Caves не подключился к Master за {timeout} секунд",
            "health": health,
        }


async def start_shard(
    shard: str = "Master",
    *,
    _auto_caves: bool = True,
    strict_caves_link: bool = True,
    _apply_world: bool = True,
) -> dict:
    shard = normalize_shard(shard)
    repair = ensure_shard_link_config()

    if shard == "Caves":
        bind_caves_to_master()

    existing = get_shard_status(shard)
    if existing.get("running"):
        status = existing
        health = get_shard_runtime_health()
        if repair.get("changed"):
            return {
                "success": False,
                "needs_restart": True,
                "already_running": True,
                "pid": status.get("pid"),
                "error": (
                    f"{shard} запущен со старым конфигом шардов. "
                    "Остановите Master и Caves, затем запустите кластер заново "
                    "(кнопка «Запустить кластер»)."
                ),
                "config_repaired": True,
                "repair_items": repair.get("items", []),
                "shard_health": health,
            }
        msg = f"{shard} уже запущен"
        if shard == "Master" and health.get("master_running") and not health.get("caves_running"):
            msg += ". Запустите Caves — без него пещеры и порталы не работают"
        return {
            "success": True,
            "already_running": True,
            "pid": status.get("pid"),
            "message": msg,
            "config_repaired": False,
            "shard_health": health,
        }

    binding = get_cluster_binding_status()
    if shard == "Master" and not binding["master"]["synced"]:
        bind_master_to_cluster()
        binding = get_cluster_binding_status()
    elif shard == "Caves" and not binding["caves"]["synced"]:
        bind_master_to_cluster()
        binding = get_cluster_binding_status()
        if binding["master"]["running"] and binding["master"]["synced"]:
            bind_caves_to_master()
            binding = get_cluster_binding_status()

    health = get_shard_runtime_health()
    if shard == "Caves" and health["needs_master_restart"]:
        return {
            "success": False,
            "error": (
                "Сначала перезапустите Master: он работает с отключёнными шардами "
                "(shard_enabled был false). Панель исправила cluster.ini."
            ),
            "shard_health": health,
            "config_repaired": repair.get("changed", False),
        }

    prereq = get_shard_prerequisites(shard)
    if not prereq["ready"]:
        failed = [c for c in prereq["checks"] if c["required"] and not c["ok"]]
        hints = "; ".join(c["hint"] for c in failed)
        return {
            "success": False,
            "error": f"Не готов к запуску: {hints}",
            "checks": prereq["checks"],
        }

    if _apply_world:
        other = "Caves" if shard == "Master" else "Master"
        if not get_shard_status(other).get("running"):
            from app.services.world_library import prepare_active_world_for_cluster_start
            world_prep = prepare_active_world_for_cluster_start()
            if not world_prep.get("success"):
                return {
                    "success": False,
                    "error": world_prep.get("error", "Не удалось подготовить выбранный мир"),
                    "world_prep": world_prep,
                }

    shard_dir = _shard_dir(shard)
    if not os.path.isdir(shard_dir):
        return {"success": False, "error": f"Каталог шарда не найден: {shard_dir}"}

    cmd = [
        _dst_binary(),
        "-shard", shard,
        "-cluster", "cluster",
        "-persistent_storage_root", get_dst_dir(),
    ]

    try:
        _ensure_panel_log_dir()
        launch_path = _launch_log_path(shard)
        launch_log = open(launch_path, "a", encoding="utf-8", buffering=1)
        try:
            launch_log.write(
                f"\n\n=== Запуск {shard} {datetime.now(timezone.utc).isoformat()} ===\n"
            )
            launch_log.flush()
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=_bin_dir(),
                env=_get_dst_env(),
                stdout=launch_log,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            _PANEL_PROCESSES[shard] = {
                "pid": proc.pid,
                "process": proc,
                "started_at": datetime.now(timezone.utc),
            }

            verified = await _verify_shard_started(shard, proc.pid)
            await asyncio.sleep(0.5)
            if not verified["ok"]:
                _PANEL_PROCESSES.pop(shard, None)
                if proc.returncode is None:
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass
                return {
                    "success": False,
                    "error": verified.get("error", "Запуск не подтверждён"),
                    "log_tail": verified.get("log_tail", []),
                    "checks": prereq["checks"],
                }

            pid = verified["pid"]
            shard_registry.set_pid(shard, pid)
            _PANEL_PROCESSES[shard]["pid"] = pid
            result = {
                "success": True,
                "pid": pid,
                "verified": True,
                "message": f"{shard} запущен (PID {pid})",
                "config_repaired": repair.get("changed", False),
            }
            if shard == "Caves":
                link = await _verify_caves_linked_to_master()
                result["shard_health"] = link.get("health") or get_shard_runtime_health()
                if link["ok"]:
                    result["message"] = f"Caves запущен (PID {pid}) и подключён к Master"
                    result["caves_linked"] = True
                elif link.get("health", {}).get("caves_running"):
                    result["caves_linked"] = False
                    result["warning"] = link.get("error")
                    result["message"] = (
                        f"Caves запущен (PID {pid}). Связь с Master может занять до 1–2 мин "
                        "(Caves переподключается автоматически)."
                    )
                elif not strict_caves_link and is_shard_running("Caves"):
                    result["caves_linked"] = False
                    result["warning"] = link.get("error")
                    result["message"] = (
                        f"Caves запущен (PID {pid}). Связь с Master устанавливается."
                    )
                else:
                    result["success"] = False
                    result["error"] = link.get("error", "Caves не подключился к Master")
                    result["log_tail"] = (
                        _get_log_tail("Caves", 25) + ["--- Master ---"] + _get_log_tail("Master", 25)
                    )
                    result["caves_linked"] = False
                    proc_info = _PANEL_PROCESSES.pop(shard, None)
                    proc = proc_info.get("process") if proc_info else None
                    try:
                        if proc and proc.returncode is None:
                            proc.kill()
                            await proc.wait()
                    except Exception:
                        pass
                    await _terminate_pids([pid])
                    shard_registry.clear_shard(shard)
            elif shard == "Master":
                result["shard_health"] = get_shard_runtime_health()
            return result
        finally:
            try:
                launch_log.close()
            except Exception:
                pass
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _terminate_pids(pids: List[int]) -> None:
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    await asyncio.sleep(3)
    for pid in pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            pass


async def stop_shard(shard: str = "Master") -> dict:
    shard = normalize_shard(shard)
    pids: List[int] = []

    # Обнаружить процесс, если реестр пуст (например после рестарта панели)
    status = get_shard_status(shard)
    if status.get("pid"):
        pids.append(int(status["pid"]))

    record = shard_registry.get_record(shard)
    if record and record.get("pid"):
        reg_pid = int(record["pid"])
        if reg_pid not in pids:
            pids.append(reg_pid)

    if shard in _PANEL_PROCESSES:
        proc_info = _PANEL_PROCESSES[shard]
        panel_pid = proc_info.get("pid")
        try:
            proc = proc_info["process"]
            if proc.returncode is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except Exception:
            pass
        if panel_pid and panel_pid not in pids:
            pids.append(panel_pid)
        _PANEL_PROCESSES.pop(shard, None)

    pids = [pid for pid in dict.fromkeys(pids) if pid > 0]
    if not pids:
        shard_registry.clear_shard(shard)
        return {"success": False, "error": f"{shard} не запущен"}

    alive = [pid for pid in pids if pid_alive(pid)]
    if alive:
        await _terminate_pids(alive)

    shard_registry.clear_shard(shard)
    return {"success": True, "stopped": pids}


async def restart_shard(shard: str = "Master") -> dict:
    await stop_shard(shard)
    await asyncio.sleep(2)
    return await start_shard(shard)


def _find_dst_shard_pids() -> dict:
    """Процессы DST по командной строке (в т.ч. вне реестра панели)."""
    return shard_registry.discover_shard_pids()


async def _stop_all_dst_processes(*, verify: bool = False) -> dict:
    """Остановить все шарды: systemd, панель, процессы вне реестра."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "stop", "dst-master.service", "dst-caves.service",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=20)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except Exception:
        pass

    await stop_cluster_shards()

    orphans = _find_dst_shard_pids()
    extra = list(dict.fromkeys(orphans["Master"] + orphans["Caves"]))
    if extra:
        await _terminate_pids(extra)
        await asyncio.sleep(1)

    still_running = [
        shard for shard in ("Master", "Caves")
        if is_shard_running(shard)
    ]
    if verify and still_running:
        return {
            "ok": False,
            "error": (
                "Не удалось остановить шарды: "
                + ", ".join(still_running)
                + ". Остановите их вручную и повторите."
            ),
            "still_running": still_running,
            "stopped_orphans": extra,
            "orphans": orphans,
        }

    shard_registry.save_registry(shard_registry._empty_registry())
    return {
        "ok": True,
        "stopped_orphans": extra,
        "orphans": orphans,
        "still_running": still_running,
    }


async def ensure_shards_stopped_for_backup() -> dict:
    """Полная остановка DST перед бэкапом или восстановлением."""
    return await _stop_all_dst_processes(verify=True)


async def _ensure_cluster_stopped() -> dict:
    """Остановить все шарды перед чистым запуском кластера."""
    result = await _stop_all_dst_processes(verify=False)
    return {
        "stopped_orphans": result.get("stopped_orphans", []),
        "orphans": result.get("orphans", {}),
    }


async def stop_cluster_shards() -> dict:
    caves = await stop_shard("Caves")
    master = await stop_shard("Master")
    return {
        "success": bool(caves.get("success") or master.get("success")),
        "caves": caves,
        "master": master,
    }


async def start_cluster_shards() -> dict:
    bindings = apply_cluster_bindings()
    if not bindings.get("success"):
        return {
            "success": False,
            "error": bindings.get("error", "Ошибка привязки конфигов"),
            "bindings": bindings,
        }

    config_check = validate_cluster_config()
    if not config_check["ok"]:
        return {
            "success": False,
            "error": "Конфиг не готов к запуску: " + "; ".join(config_check["errors"]),
            "hints": config_check["hints"],
            "bindings": bindings,
        }

    await _ensure_cluster_stopped()
    await asyncio.sleep(3)

    from app.services.world_library import prepare_active_world_for_cluster_start
    world_prep = prepare_active_world_for_cluster_start()
    if not world_prep.get("success"):
        return {
            "success": False,
            "error": world_prep.get("error", "Не удалось подготовить выбранный мир"),
            "world_prep": world_prep,
            "bindings": bindings,
        }

    master = await start_shard("Master", _auto_caves=False, _apply_world=False)
    if not master.get("success"):
        return {
            "success": False,
            "error": master.get("error", "Не удалось запустить Master"),
            "master": master,
            "bindings": bindings,
            "world_prep": world_prep,
        }

    await asyncio.sleep(MASTER_WARMUP_SECONDS)
    caves = await start_shard("Caves", _auto_caves=False, _apply_world=False)
    health = get_shard_runtime_health()
    caves_running = bool(caves.get("success"))
    caves_linked = bool(caves.get("caves_linked") or health.get("caves_linked"))

    if caves_running and caves_linked:
        message = "Кластер запущен: Master и Caves подключены"
        success = True
    elif caves_running:
        message = (
            "Master и Caves запущены. Caves подключается к Master "
            "(обычно до 1–2 мин). Статус — на вкладке «Сервер»."
        )
        if caves.get("warning"):
            message += " " + caves["warning"]
        success = True
    else:
        message = caves.get("error", "Caves не запустился")
        success = False

    if world_prep.get("world_name"):
        message = f"{message} (мир: {world_prep['world_name']})"
    elif world_prep.get("mode") == "new":
        message = f"{message} (новый мир)"

    return {
        "success": success,
        "message": message,
        "master": master,
        "caves": caves,
        "shard_health": health,
        "bindings": bindings,
        "world_prep": world_prep,
        "config_on_disk": _cluster_config_snapshot(),
    }


def _cluster_config_snapshot() -> dict:
    """Краткая сводка cluster.ini для диагностики."""
    cluster = read_cluster_ini()
    return {
        "shard_enabled": cluster.get("SHARD.shard_enabled"),
        "master_port": cluster.get("SHARD.master_port"),
        "master_ip": cluster.get("SHARD.master_ip"),
        "cluster_key": cluster.get("SHARD.cluster_key"),
        "bind_ip": cluster.get("SHARD.bind_ip"),
    }


async def restart_cluster_shards() -> dict:
    return await start_cluster_shards()


def _set_regen_state(
    *,
    step: str,
    percent: int,
    message: str,
    error: Optional[str] = None,
    details: Optional[dict] = None,
    active: bool = True,
    finished: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    if _REGEN_STATE.get("started_at") is None and active:
        _REGEN_STATE["started_at"] = now
    _REGEN_STATE.update({
        "active": active and not finished,
        "step": step,
        "percent": max(0, min(100, percent)),
        "message": message,
        "error": error,
        "details": details or _REGEN_STATE.get("details") or {},
    })
    if finished:
        _REGEN_STATE["active"] = False
        _REGEN_STATE["finished_at"] = now


def get_regenerate_world_status() -> dict:
    return dict(_REGEN_STATE)


def _rmtree_safe(path: str) -> None:
    if not os.path.exists(path):
        return

    def _onerror(func, item_path, _exc_info):
        try:
            os.chmod(item_path, 0o700)
            func(item_path)
        except Exception:
            pass

    shutil.rmtree(path, onerror=_onerror)


def _session_roots(shard_dir: str) -> List[str]:
    return [
        os.path.join(shard_dir, "session"),
        os.path.join(shard_dir, "Save", "session"),
        os.path.join(shard_dir, "save", "session"),
    ]


def _world_save_targets(shard: str) -> List[dict]:
    """Все пути сейва DST, которые нужно убрать для пересоздания мира."""
    shard = normalize_shard(shard)
    shard_dir = _shard_dir(shard)
    targets: List[dict] = []
    seen = set()

    def add(path: str, kind: str) -> None:
        real = os.path.realpath(path)
        if real in seen or not os.path.exists(path):
            return
        seen.add(real)
        targets.append({
            "path": path,
            "kind": kind,
            "is_dir": os.path.isdir(path),
            "size": _path_size(path),
        })

    for root in _session_roots(shard_dir):
        add(root, "session")
    for sub in ("Save", "save", "backup"):
        add(os.path.join(shard_dir, sub), sub)
    for name in ("saveindex", "SaveIndex"):
        add(os.path.join(shard_dir, name), "saveindex")

    return targets


def _path_size(path: str) -> int:
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _archive_path(path: str, tag: str) -> Optional[str]:
    if not os.path.exists(path):
        return None
    backup = f"{path}.regen.{tag}"
    try:
        if os.path.exists(backup):
            _rmtree_safe(backup) if os.path.isdir(backup) else os.remove(backup)
        shutil.move(path, backup)
        return backup
    except OSError:
        return None


def _clear_world_save_data() -> dict:
    """Удалить все данные мира Master/Caves (с бэкапом на диске)."""
    refresh_dst_paths()
    tag = str(int(time.time()))
    cleared: List[dict] = []
    errors: List[str] = []
    scanned: List[dict] = []

    for shard in ("Master", "Caves"):
        shard_dir = _shard_dir(shard)
        if not os.path.isdir(shard_dir):
            errors.append(f"{shard}: каталог не найден ({shard_dir})")
            continue

        for item in _world_save_targets(shard):
            scanned.append({"shard": shard, **item})

        for log_name in ("server_log.txt", "server_chat_log.txt", "client_log.txt"):
            log_path = os.path.join(shard_dir, log_name)
            archived = _archive_path(log_path, tag)
            if archived:
                cleared.append({"shard": shard, "kind": "log", "backup": archived})

        for item in _world_save_targets(shard):
            src = item["path"]
            backup = _archive_path(src, tag)
            if backup:
                cleared.append({
                    "shard": shard,
                    "kind": item["kind"],
                    "backup": backup,
                    "bytes": item.get("size", 0),
                })
            else:
                errors.append(f"{shard}: не удалось архивировать {src}")

    return {
        "cleared": cleared,
        "errors": errors,
        "scanned": scanned,
        "had_save_data": bool(scanned),
    }


def _log_since_last_panel_launch(shard: str) -> str:
    path = _launch_log_path(shard)
    lines = _read_log_file(path, 800)
    if not lines:
        return ""
    last = 0
    for idx, line in enumerate(lines):
        if _RE_PANEL_LAUNCH.search(line):
            last = idx
    return "\n".join(lines[last:])


def _has_fresh_session_folder(shard: str, since_ts: float) -> bool:
    shard_dir = _shard_dir(shard)
    for root in _session_roots(shard_dir):
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder):
                continue
            try:
                if os.path.getmtime(folder) >= since_ts - 10:
                    return True
            except OSError:
                continue
    return False


async def _wait_shard_world_ready(
    shard: str,
    *,
    since_ts: float,
    timeout: int = 300,
) -> dict:
    shard = normalize_shard(shard)
    for elapsed in range(timeout):
        await asyncio.sleep(1)
        if not is_shard_running(shard):
            return {
                "ok": False,
                "error": f"{shard} завершился во время генерации мира",
                "log_tail": _get_log_tail(shard, 40),
            }

        launch_log = _log_since_last_panel_launch(shard)
        shard_log = "\n".join(_get_session_log_tail(shard, 500))
        combined = f"{launch_log}\n{shard_log}"

        if _RE_WORLD_READY.search(combined):
            return {"ok": True, "elapsed": elapsed + 1, "via": "log"}
        if _has_fresh_session_folder(shard, since_ts):
            return {"ok": True, "elapsed": elapsed + 1, "via": "session"}

        if _RE_WORLD_GENERATED.search(combined):
            pct_hint = min(88, 45 + elapsed // 4)
            _set_regen_state(
                step=f"generating_{shard.lower()}",
                percent=pct_hint,
                message=f"{shard}: генерация мира... ({elapsed + 1} с)",
            )

    return {
        "ok": False,
        "error": (
            f"{shard} не подтвердил готовность за {timeout} с. "
            "Проверьте логи — возможно мир создан, но панель не увидела сигнал."
        ),
        "log_tail": _get_log_tail(shard, 50),
        "launch_tail": _read_log_file(_launch_log_path(shard), 40),
    }


async def _regenerate_world_worker() -> None:
    global _regen_task
    cleared: dict = {}
    try:
        refresh_dst_paths()
        config_check = validate_cluster_config()
        if not config_check["ok"]:
            raise RuntimeError(
                "Конфиг не готов: " + "; ".join(config_check.get("errors", []))
            )

        _set_regen_state(
            step="stopping",
            percent=8,
            message="Остановка кластера...",
            details={},
        )
        stop = await ensure_shards_stopped_for_backup()
        if not stop.get("ok"):
            raise RuntimeError(stop.get("error", "Не удалось остановить кластер"))
        await asyncio.sleep(3)

        _set_regen_state(
            step="clearing",
            percent=22,
            message="Архивирование и удаление сохранений мира...",
        )
        cleared = _clear_world_save_data()
        if cleared.get("errors"):
            raise RuntimeError("; ".join(cleared["errors"]))

        if not cleared.get("had_save_data"):
            _set_regen_state(
                step="clearing",
                percent=25,
                message="Старых сейвов не найдено — будет создан новый мир",
                details={"cleared": cleared.get("cleared", []), "scanned": []},
            )

        cluster_started_at = time.time()
        _set_regen_state(
            step="starting_master",
            percent=38,
            message="Запуск Master...",
            details={"cleared": cleared.get("cleared", [])},
        )

        bindings = apply_cluster_bindings()
        if not bindings.get("success"):
            raise RuntimeError(bindings.get("error", "Ошибка привязки конфигов"))

        master = await start_shard("Master", _auto_caves=False)
        if not master.get("success"):
            if master.get("already_running"):
                stop = await ensure_shards_stopped_for_backup()
                if not stop.get("ok"):
                    raise RuntimeError(stop.get("error", "Master всё ещё запущен"))
                master = await start_shard("Master", _auto_caves=False)
            if not master.get("success"):
                raise RuntimeError(master.get("error", "Не удалось запустить Master"))

        master_ready = await _wait_shard_world_ready(
            "Master", since_ts=cluster_started_at, timeout=360
        )
        if not master_ready.get("ok"):
            raise RuntimeError(master_ready.get("error", "Master не сгенерировал мир"))

        _set_regen_state(
            step="starting_caves",
            percent=68,
            message="Запуск Caves...",
        )
        await asyncio.sleep(MASTER_WARMUP_SECONDS)

        caves_started_at = time.time()
        caves = await start_shard(
            "Caves", _auto_caves=False, strict_caves_link=False
        )
        if not caves.get("success"):
            if is_shard_running("Caves"):
                caves = {
                    "success": True,
                    "warning": caves.get("error"),
                    "message": "Caves запущен, ожидание генерации мира",
                }
            elif caves.get("already_running"):
                await stop_shard("Caves")
                await asyncio.sleep(2)
                caves = await start_shard(
                    "Caves", _auto_caves=False, strict_caves_link=False
                )
            if not caves.get("success") and not is_shard_running("Caves"):
                raise RuntimeError(caves.get("error", "Не удалось запустить Caves"))

        caves_ready = await _wait_shard_world_ready(
            "Caves", since_ts=caves_started_at, timeout=360
        )
        if not caves_ready.get("ok") and not is_shard_running("Caves"):
            raise RuntimeError(caves_ready.get("error", "Caves не сгенерировал мир"))

        _set_regen_state(
            step="linking",
            percent=88,
            message="Ожидание связи Caves с Master (до 2 мин)...",
        )
        link = await _verify_caves_linked_to_master(timeout_seconds=120)
        health = link.get("health") or get_shard_runtime_health()

        details = {
            "cleared": cleared.get("cleared", []),
            "master": master,
            "caves": caves,
            "master_ready": master_ready,
            "caves_ready": caves_ready,
            "shard_health": health,
        }

        if link.get("ok") or health.get("caves_linked"):
            _set_regen_state(
                step="done",
                percent=100,
                message="Мир пересобран: новый мир Master + Caves готов",
                details=details,
                finished=True,
            )
        elif health.get("caves_running") and health.get("master_running"):
            _set_regen_state(
                step="done",
                percent=100,
                message=(
                    "Мир пересобран. Caves ещё подключается к Master — "
                    "подождите 1–2 мин и проверьте статус."
                ),
                details={**details, "warning": link.get("error")},
                finished=True,
            )
        else:
            raise RuntimeError(link.get("error", "Caves не подключился к Master"))
    except Exception as exc:
        _set_regen_state(
            step="error",
            percent=_REGEN_STATE.get("percent", 0),
            message="Ошибка пересборки мира",
            error=str(exc),
            details={"cleared": cleared.get("cleared", [])},
            finished=True,
        )
    finally:
        _regen_task = None


async def start_regenerate_world() -> dict:
    global _regen_task
    async with _REGEN_LOCK:
        if _REGEN_STATE.get("active"):
            return {
                "success": False,
                "error": "Пересборка мира уже выполняется",
                "status": get_regenerate_world_status(),
            }
        if _regen_task and not _regen_task.done():
            return {
                "success": False,
                "error": "Пересборка мира уже выполняется",
                "status": get_regenerate_world_status(),
            }

        refresh_dst_paths()
        config_check = validate_cluster_config()
        if not config_check["ok"]:
            return {
                "success": False,
                "error": "Конфиг не готов: " + "; ".join(config_check.get("errors", [])),
                "hints": config_check.get("hints", []),
            }

        _REGEN_STATE.clear()
        _REGEN_STATE.update({
            "active": True,
            "step": "queued",
            "percent": 0,
            "message": "Запуск пересборки мира...",
            "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "details": {},
        })
        _regen_task = asyncio.create_task(_regenerate_world_worker())

    return {
        "success": True,
        "message": "Пересборка мира запущена",
        "status": get_regenerate_world_status(),
    }


def is_shard_directory_ready(shard: str = "Master") -> bool:
    shard = normalize_shard(shard)
    return os.path.isdir(_shard_dir(shard)) and os.path.isfile(f"{_shard_dir(shard)}/server.ini")


async def _run_cmd(cmd: list, cwd: str = None) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd or get_dst_dir(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return {
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace"),
        "stderr": stderr.decode("utf-8", errors="replace"),
    }


async def install_steamcmd() -> dict:
    try:
        os.makedirs(_steamcmd_dir(), exist_ok=True)
        result = await _run_cmd(
            ["wget", "-O", f"{_steamcmd_dir()}/steamcmd_linux.tar.gz",
             "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"],
            cwd=_steamcmd_dir(),
        )
        if result["returncode"] != 0:
            return {"success": False, "error": result["stderr"]}

        result = await _run_cmd(
            ["tar", "-xzf", f"{_steamcmd_dir()}/steamcmd_linux.tar.gz"],
            cwd=_steamcmd_dir(),
        )
        if result["returncode"] != 0:
            return {"success": False, "error": result["stderr"]}

        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def install_dst_server(force: bool = False) -> dict:
    steamcmd = f"{_steamcmd_dir()}/steamcmd.sh"
    if not os.path.exists(steamcmd):
        inst = await install_steamcmd()
        if not inst["success"]:
            return inst

    os.makedirs(f"{get_dst_dir()}/steamapps", exist_ok=True)

    base_cmd = [
        steamcmd,
        "+@sSteamCmdForcePlatformType", "linux",
        "+@NoPromptForPassword", "1",
        "+@ShutdownOnFailedCommand", "0",
        "+force_install_dir", get_dst_dir(),
        "+login", "anonymous",
        "+app_update", "343050",
    ]
    if force:
        base_cmd.append("validate")
    base_cmd.append("+quit")

    last_result = None
    for attempt in range(1, 5):
        result = await _run_cmd(base_cmd)
        last_result = result
        if os.path.exists(_dst_binary()):
            return {
                "success": True,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "attempts": attempt,
            }
        await asyncio.sleep(5)

    return {
        "success": False,
        "stdout": last_result["stdout"] if last_result else "",
        "stderr": last_result["stderr"] if last_result else "",
        "error": "DST server binary not found after multiple SteamCMD attempts",
    }


async def update_server() -> dict:
    return await install_dst_server(force=True)


def create_cluster_structure(cluster_name: str = "My DST Server") -> dict:
    from app.config.config_reader import ensure_default_configs
    return ensure_default_configs(cluster_name)


def get_logs(shard: str = "Master", lines: int = 100) -> dict:
    return _collect_log_lines(shard, lines)


def get_server_overview() -> dict:
    refresh_dst_paths()
    master_status = get_shard_status("Master")
    caves_status = get_shard_status("Caves")
    health = get_shard_runtime_health(master_status, caves_status)
    master_prereq = get_shard_prerequisites("Master")
    caves_prereq = get_shard_prerequisites("Caves")
    binding = get_cluster_binding_status()
    warnings = []

    for msg in health.get("messages", []):
        if "подключён к Master" not in msg:
            warnings.append({"type": "shard_health", "message": msg})

    if caves_status["running"] and not binding["caves"]["synced"]:
        warnings.append({
            "type": "caves_unlinked",
            "message": (
                "Caves работает без привязки к Master (нет SHARD.master_ip). "
                "Остановите Caves, привяжите в Конфиг → Caves и запустите снова."
            ),
        })

    if master_status["running"] and not caves_status["running"]:
        warnings.append({
            "type": "caves_not_running",
            "message": (
                "Запущен только Master. Запустите Caves или «Запустить кластер» — "
                "иначе пещеры и порталы не работают, игроки могут вылетать при входе."
            ),
        })
    elif (
        master_status["running"]
        and caves_status["running"]
        and not health["caves_linked"]
    ):
        warnings.append({
            "type": "caves_not_linked",
            "message": (
                "Caves не подключён к Master (no available shard [Caves]). "
                "Нажмите «Перезапустить кластер»."
            ),
        })
    elif (
        master_status["running"]
        and caves_status["running"]
        and health["caves_linked"]
    ):
        pass
    elif master_status["running"] and caves_status["running"]:
        warnings.append({
            "type": "shards_misconfigured",
            "message": (
                "Оба шарда запущены, но конфиг не синхронизирован — "
                "в списке Klei может быть два сервера с одним именем."
            ),
        })

    return {
        "master": {**master_status, "prerequisites": master_prereq},
        "caves": {**caves_status, "prerequisites": caves_prereq},
        "binding": binding,
        "shard_health": health,
        "warnings": warnings,
    }


def __getattr__(name: str):
    """Динамические пути DST для обратной совместимости импортов."""
    dynamic = {
        "DST_DIR": get_dst_dir,
        "CLUSTER_DIR": get_cluster_dir,
        "BIN_DIR": _bin_dir,
        "STEAMCMD_DIR": _steamcmd_dir,
        "MODS_DIR": _mods_dir,
        "DST_BINARY": _dst_binary,
    }
    if name in dynamic:
        refresh_dst_paths()
        return dynamic[name]()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
