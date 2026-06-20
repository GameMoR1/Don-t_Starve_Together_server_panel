import configparser
import re
import os
import shutil
from typing import Optional

def refresh_dst_paths() -> dict:
    """Перечитывает DST_DIR из окружения (важно после .env / install.sh)."""
    global DST_DIR, KLEI_DIR, CLUSTER_DIR
    DST_DIR = os.environ.get("DST_DIR", "/home/dstpanel/dst")
    KLEI_DIR = os.path.join(DST_DIR, "DoNotStarveTogether")
    CLUSTER_DIR = os.path.join(KLEI_DIR, "cluster")
    return {
        "dst_dir": DST_DIR,
        "klei_dir": KLEI_DIR,
        "cluster_dir": CLUSTER_DIR,
    }


def get_dst_dir() -> str:
    return refresh_dst_paths()["dst_dir"]


def get_cluster_dir() -> str:
    return refresh_dst_paths()["cluster_dir"]


refresh_dst_paths()
TOKEN_BACKUP_PATH = "/var/lib/dst-panel/.cluster_token.backup"
ALLOWED_CONFIG_FILES = {
    "cluster.ini": CLUSTER_DIR,
    "server.ini": "{shard_dir}",
    "modoverrides.lua": CLUSTER_DIR,
    "adminlist.txt": CLUSTER_DIR,
    "blocklist.txt": CLUSTER_DIR,
    "whitelist.txt": CLUSTER_DIR,
    "cluster_token.txt": CLUSTER_DIR,
}

ALLOWED_PATHS = set(ALLOWED_CONFIG_FILES.values())

_SHARD_NAMES = {"master": "Master", "caves": "Caves"}


def normalize_shard(shard: str) -> str:
    return _SHARD_NAMES.get(shard.lower(), shard)


def resolve_safe_path(filepath: str) -> Optional[str]:
    resolved = os.path.realpath(filepath)
    real_cluster = os.path.realpath(CLUSTER_DIR)
    if not resolved.startswith(real_cluster):
        return None
    for allowed in ALLOWED_PATHS:
        if allowed == "{shard_dir}":
            for shard in ["Master", "Caves"]:
                shard_dir = f"{CLUSTER_DIR}/{shard}"
                if resolved.startswith(os.path.realpath(shard_dir)):
                    return resolved
            continue
        if resolved.startswith(os.path.realpath(allowed)):
            return resolved
    return None


def validate_path(filepath: str) -> bool:
    return resolve_safe_path(filepath) is not None


def _normalize_cluster_dict(data: dict) -> dict:
    """Klei использует shard_enabled, не enabled (см. settings guide)."""
    out = dict(data)
    if "SHARD.enabled" in out:
        if "SHARD.shard_enabled" not in out:
            out["SHARD.shard_enabled"] = out["SHARD.enabled"]
        out.pop("SHARD.enabled", None)
    return out


def cluster_shards_enabled(cluster: dict) -> bool:
    cluster = _normalize_cluster_dict(cluster)
    return cluster.get("SHARD.shard_enabled", "false").lower() == "true"


def _ensure_cluster_shard_keys(cluster: dict) -> bool:
    changed = False
    if "SHARD.enabled" in cluster:
        if "SHARD.shard_enabled" not in cluster:
            cluster["SHARD.shard_enabled"] = cluster["SHARD.enabled"]
        cluster.pop("SHARD.enabled", None)
        changed = True
    if str(cluster.get("SHARD.shard_enabled", "false")).lower() != "true":
        cluster["SHARD.shard_enabled"] = "true"
        changed = True
    if not cluster.get("SHARD.master_port"):
        cluster["SHARD.master_port"] = "10888"
        changed = True
    if cluster.get("SHARD.master_ip", "") in ("", "0.0.0.0"):
        cluster["SHARD.master_ip"] = "127.0.0.1"
        changed = True
    if not cluster.get("SHARD.bind_ip"):
        cluster["SHARD.bind_ip"] = "0.0.0.0"
        changed = True
    if not cluster.get("SHARD.cluster_key"):
        cluster["SHARD.cluster_key"] = "default"
        changed = True
    return changed


def read_cluster_ini() -> dict:
    path = f"{CLUSTER_DIR}/cluster.ini"
    if not os.path.exists(path):
        return _default_cluster_ini()
    config = configparser.ConfigParser()
    config.read(path)
    result = {}
    for section in config.sections():
        for key, value in config.items(section):
            result[f"{section}.{key}"] = value
    return _normalize_cluster_dict(result)


def _default_cluster_ini() -> dict:
    return {
        "GAMEPLAY.max_players": "6",
        "GAMEPLAY.pvp": "false",
        "GAMEPLAY.game_mode": "survival",
        "NETWORK.cluster_name": "My DST Server",
        "NETWORK.cluster_description": "",
        "NETWORK.cluster_password": "",
        "NETWORK.offline_cluster": "false",
        "NETWORK.lan_only_cluster": "false",
        "NETWORK.cluster_language": "english",
        "MISC.console_enabled": "true",
        "MISC.tick_rate": "15",
        "SHARD.shard_enabled": "true",
        "SHARD.bind_ip": "0.0.0.0",
        "SHARD.master_ip": "127.0.0.1",
        "SHARD.master_port": "10888",
        "SHARD.cluster_key": "default",
    }


def write_cluster_ini(data: dict) -> dict:
    path = f"{CLUSTER_DIR}/cluster.ini"
    data = _normalize_cluster_dict(data)
    config = configparser.ConfigParser()
    config.optionxform = str
    sections = {}
    for key, value in data.items():
        if "." in key:
            section, option = key.split(".", 1)
            if section not in sections:
                sections[section] = {}
            sections[section][option] = str(value)
    for section, options in sections.items():
        config[section] = options
    try:
        with open(path, "w") as f:
            config.write(f, space_around_delimiters=False)
        if "SHARD.master_port" in data:
            _sync_caves_master_port(data["SHARD.master_port"])
        elif any(k.startswith("SHARD.") for k in data):
            _sync_caves_shard_link()
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _sync_caves_shard_link(cluster: dict = None) -> None:
    if cluster is None:
        cluster = read_cluster_ini()
    caves_path = f"{CLUSTER_DIR}/Caves/server.ini"
    if not os.path.exists(caves_path):
        return
    caves = read_shard_ini("Caves")
    master_ip = cluster.get("SHARD.master_ip", "127.0.0.1")
    master_shard_port = cluster.get("SHARD.master_port", "10888")
    caves["SHARD.is_master"] = "false"
    caves["SHARD.name"] = "Caves"
    caves["SHARD.id"] = "2"
    caves["SHARD.master_ip"] = str(master_ip)
    caves["SHARD.master_port"] = str(master_shard_port)
    caves["SHARD.cluster_key"] = str(cluster.get("SHARD.cluster_key", "default"))
    write_shard_ini("Caves", caves)


def _sync_caves_master_port(master_shard_port: str) -> None:
    cluster = read_cluster_ini()
    cluster["SHARD.master_port"] = str(master_shard_port)
    _sync_caves_shard_link(cluster)


MASTER_WORLDGEN = """return {
\toverride_enabled = true,
\tpreset_type = "SURVIVAL_TOGETHER",
\toverrides = {},
}
"""

CAVES_WORLDGEN = """return {
\toverride_enabled = true,
\tpreset_type = "SURVIVAL_TOGETHER",
\toverrides = {
\t\tleveltype = "CAVE",
\t},
}
"""


def ensure_worldgen_files() -> dict:
    """Без worldgenoverride.lua Caves-шард не поднимается корректно."""
    created = []
    for shard, content in (("Master", MASTER_WORLDGEN), ("Caves", CAVES_WORLDGEN)):
        path = f"{CLUSTER_DIR}/{shard}/worldgenoverride.lua"
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        created.append(f"{shard}/worldgenoverride.lua")
    return {"changed": bool(created), "items": created}


def ensure_shard_link_config() -> dict:
    """Чинит типичную misconfig: Caves регистрируется в браузере как второй сервер."""
    if not os.path.isfile(f"{CLUSTER_DIR}/cluster.ini"):
        return {"changed": False, "items": []}

    cluster = read_cluster_ini()
    changed = []

    cluster_path = f"{CLUSTER_DIR}/cluster.ini"
    if os.path.isfile(cluster_path):
        try:
            with open(cluster_path, "r", encoding="utf-8") as handle:
                raw_ini = handle.read()
            if re.search(r"(?m)^enabled\s*=", raw_ini) and "shard_enabled" not in raw_ini:
                changed.append("cluster.migrate_enabled_to_shard_enabled")
        except OSError:
            pass

    if _ensure_cluster_shard_keys(cluster):
        changed.append("cluster.shard_enabled")
    if changed:
        write_cluster_ini(cluster)

    master_ip = cluster.get("SHARD.master_ip", "127.0.0.1")
    master_shard_port = cluster.get("SHARD.master_port", "10888")

    master = read_shard_ini("Master")
    master_updates = {}
    master_dirty = False
    if master.get("SHARD.is_master", "").lower() != "true":
        master_updates["SHARD.is_master"] = "true"
    if master.get("SHARD.name", "") != "Master":
        master_updates["SHARD.name"] = "Master"
    if str(master.get("SHARD.id", "")) != "1":
        master_updates["SHARD.id"] = "1"
    for bad_key in ("SHARD.master_ip", "SHARD.master_port"):
        if bad_key in master:
            master.pop(bad_key, None)
            changed.append(f"master.remove_{bad_key}")
            master_dirty = True
    if master_updates:
        master.update(master_updates)
        changed.extend(f"master.{k}" for k in master_updates)
        master_dirty = True
    if master_dirty:
        write_shard_ini("Master", master)

    caves = read_shard_ini("Caves")
    caves_updates = {}
    if caves.get("SHARD.is_master", "").lower() != "false":
        caves_updates["SHARD.is_master"] = "false"
    if caves.get("SHARD.name", "") != "Caves":
        caves_updates["SHARD.name"] = "Caves"
    if str(caves.get("SHARD.id", "")) != "2":
        caves_updates["SHARD.id"] = "2"
    if caves.get("SHARD.master_ip") != master_ip:
        caves_updates["SHARD.master_ip"] = master_ip
    if str(caves.get("SHARD.master_port", "")) != str(master_shard_port):
        caves_updates["SHARD.master_port"] = str(master_shard_port)
    cluster_key = cluster.get("SHARD.cluster_key", "default")
    if str(caves.get("SHARD.cluster_key", "")) != str(cluster_key):
        caves_updates["SHARD.cluster_key"] = str(cluster_key)

    master_port = master.get("NETWORK.server_port", "10999")
    caves_port = caves.get("NETWORK.server_port", "11000")
    if str(master_port) == str(caves_port):
        caves_updates["NETWORK.server_port"] = (
            "11000" if str(master_port) != "11000" else "11001"
        )

    if caves_updates:
        caves.update(caves_updates)
        write_shard_ini("Caves", caves)
        changed.extend(f"caves.{k}" for k in caves_updates)

    wg = ensure_worldgen_files()
    if wg.get("changed"):
        changed.extend(wg.get("items", []))

    return {"changed": bool(changed), "items": changed}


def read_shard_ini(shard: str = "Master") -> dict:
    shard = normalize_shard(shard)
    path = f"{CLUSTER_DIR}/{shard}/server.ini"
    if not os.path.exists(path):
        return _default_shard_ini(shard)
    config = configparser.ConfigParser()
    config.read(path)
    result = {}
    for section in config.sections():
        for key, value in config.items(section):
            result[f"{section}.{key}"] = value
    return result


def _online_cluster_ini(cluster_name: str = "Мой DST сервер", password: str = "") -> dict:
    return {
        "GAMEPLAY.max_players": "6",
        "GAMEPLAY.pvp": "false",
        "GAMEPLAY.game_mode": "survival",
        "NETWORK.cluster_name": cluster_name,
        "NETWORK.cluster_description": "",
        "NETWORK.cluster_password": password,
        "NETWORK.offline_cluster": "false",
        "NETWORK.lan_only_cluster": "false",
        "NETWORK.cluster_language": "russian",
        "MISC.console_enabled": "true",
        "MISC.tick_rate": "15",
        "SHARD.shard_enabled": "true",
        "SHARD.bind_ip": "0.0.0.0",
        "SHARD.master_ip": "127.0.0.1",
        "SHARD.master_port": "10888",
        "SHARD.cluster_key": "default",
    }


def _friends_cluster_ini(cluster_name: str = "Игра с друзьями", password: str = "") -> dict:
    return {
        "GAMEPLAY.max_players": "4",
        "GAMEPLAY.pvp": "false",
        "GAMEPLAY.game_mode": "survival",
        "NETWORK.cluster_name": cluster_name,
        "NETWORK.cluster_description": "Приватный сервер для друзей",
        "NETWORK.cluster_password": password,
        "NETWORK.offline_cluster": "true",
        "NETWORK.lan_only_cluster": "false",
        "NETWORK.cluster_language": "russian",
        "MISC.console_enabled": "true",
        "MISC.tick_rate": "15",
        "SHARD.shard_enabled": "true",
        "SHARD.bind_ip": "0.0.0.0",
        "SHARD.master_ip": "127.0.0.1",
        "SHARD.master_port": "10888",
        "SHARD.cluster_key": "default",
    }


def _shard_ini_master() -> dict:
    return {
        "NETWORK.server_port": "10999",
        "SHARD.is_master": "true",
        "SHARD.name": "Master",
        "SHARD.id": "1",
        "STEAM.master_server_port": "27016",
        "STEAM.authentication_port": "8766",
        "ACCOUNT.encode_user_path": "true",
    }


def _shard_ini_caves(
    master_shard_port: str = "10888",
    master_ip: str = "127.0.0.1",
    cluster_key: str = "default",
) -> dict:
    return {
        "NETWORK.server_port": "11000",
        "SHARD.is_master": "false",
        "SHARD.name": "Caves",
        "SHARD.id": "2",
        "SHARD.master_ip": str(master_ip),
        "SHARD.master_port": str(master_shard_port),
        "SHARD.cluster_key": str(cluster_key),
        "STEAM.master_server_port": "27017",
        "STEAM.authentication_port": "8767",
        "ACCOUNT.encode_user_path": "true",
    }


def build_preset_config(
    mode: str = "online",
    cluster_name: str = None,
    password: str = "",
) -> dict:
    if mode == "friends":
        name = (cluster_name or "Игра с друзьями").strip() or "Игра с друзьями"
        cluster = _friends_cluster_ini(name, password)
    else:
        name = (cluster_name or "Мой DST сервер").strip() or "Мой DST сервер"
        cluster = _online_cluster_ini(name, password)
    master_shard_port = cluster["SHARD.master_port"]
    master_ip = cluster.get("SHARD.master_ip", "127.0.0.1")
    cluster_key = cluster.get("SHARD.cluster_key", "default")
    return {
        "mode": mode,
        "cluster": cluster,
        "master": _shard_ini_master(),
        "caves": _shard_ini_caves(master_shard_port, master_ip, cluster_key),
    }


def build_friends_preset_config(
    cluster_name: str = "Игра с друзьями",
    password: str = "",
) -> dict:
    return build_preset_config("friends", cluster_name, password)


def build_online_preset_config(
    cluster_name: str = "Мой DST сервер",
    password: str = "",
) -> dict:
    return build_preset_config("online", cluster_name, password)


def _friends_shard_ini(shard: str, master_shard_port: str = "10888", master_ip: str = "127.0.0.1") -> dict:
    if shard == "Master":
        return _shard_ini_master()
    cluster = read_cluster_ini()
    return _shard_ini_caves(
        master_shard_port,
        master_ip,
        cluster.get("SHARD.cluster_key", "default"),
    )


def _default_shard_ini(shard: str) -> dict:
    cluster = _default_cluster_ini()
    return _friends_shard_ini(shard, cluster["SHARD.master_port"], cluster["SHARD.master_ip"])


def write_shard_ini(shard: str, data: dict) -> dict:
    shard = normalize_shard(shard)
    path = f"{CLUSTER_DIR}/{shard}/server.ini"
    config = configparser.ConfigParser()
    config.optionxform = str
    sections = {}
    for key, value in data.items():
        if "." in key:
            section, option = key.split(".", 1)
            if section not in sections:
                sections[section] = {}
            sections[section][option] = str(value)
    for section, options in sections.items():
        config[section] = options
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            config.write(f, space_around_delimiters=False)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_modoverrides() -> str:
    from app.services.mod_service import read_modoverrides as _read
    return _read()


def write_modoverrides(content: str) -> dict:
    from app.services.mod_service import sync_mod_files_from_overrides
    return sync_mod_files_from_overrides(content)


def read_text_file(filename: str) -> str:
    resolved = resolve_safe_path(filename)
    if not resolved:
        return None
    try:
        with open(resolved, "r") as f:
            return f.read()
    except Exception:
        return ""


def write_text_file(filename: str, content: str) -> dict:
    resolved = resolve_safe_path(filename)
    if not resolved:
        return {"success": False, "error": "Path not allowed"}
    try:
        with open(resolved, "w") as f:
            f.write(content)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _backup_cluster_token(source_path: str) -> None:
    try:
        os.makedirs(os.path.dirname(TOKEN_BACKUP_PATH), exist_ok=True)
        shutil.copy2(source_path, TOKEN_BACKUP_PATH)
        os.chmod(TOKEN_BACKUP_PATH, 0o600)
    except Exception:
        pass


def restore_cluster_token_if_missing() -> bool:
    path = f"{CLUSTER_DIR}/cluster_token.txt"
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return False
    if not os.path.exists(TOKEN_BACKUP_PATH) or os.path.getsize(TOKEN_BACKUP_PATH) == 0:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        shutil.copy2(TOKEN_BACKUP_PATH, path)
        os.chmod(path, 0o600)
        return True
    except Exception:
        return False


def read_cluster_token() -> str:
    restore_cluster_token_if_missing()
    path = f"{CLUSTER_DIR}/cluster_token.txt"
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def validate_cluster_token(token: str) -> Optional[str]:
    cleaned = (token or "").strip()
    if not cleaned:
        return "Токен не может быть пустым"
    if len(cleaned) < 10:
        return "Токен слишком короткий"
    if any(c in cleaned for c in "\n\r\t"):
        return "Токен не должен содержать переносы строк"
    return None


def write_cluster_token(token: str) -> dict:
    err = validate_cluster_token(token)
    if err:
        return {"success": False, "error": err}
    path = f"{CLUSTER_DIR}/cluster_token.txt"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(token.strip())
        os.chmod(path, 0o600)
        _backup_cluster_token(path)
        return {"success": True, "path": path}
    except Exception as e:
        return {"success": False, "error": str(e)}


def ensure_default_configs(cluster_name: str = None, password: str = None) -> dict:
    cluster_path = f"{CLUSTER_DIR}/cluster.ini"
    if not os.path.exists(cluster_path):
        return apply_online_preset(cluster_name, password)

    created = []
    try:
        os.makedirs(f"{CLUSTER_DIR}/Master", exist_ok=True)
        os.makedirs(f"{CLUSTER_DIR}/Caves", exist_ok=True)

        cluster = read_cluster_ini()
        master_shard_port = cluster.get("SHARD.master_port", "10888")
        master_ip = cluster.get("SHARD.master_ip", "127.0.0.1")

        master_path = f"{CLUSTER_DIR}/Master/server.ini"
        if not os.path.exists(master_path):
            write_shard_ini("Master", _shard_ini_master())
            created.append("Master/server.ini")

        caves_path = f"{CLUSTER_DIR}/Caves/server.ini"
        if not os.path.exists(caves_path):
            write_shard_ini("Caves", _shard_ini_caves(master_shard_port, master_ip))
            created.append("Caves/server.ini")

        mod_path = f"{CLUSTER_DIR}/modoverrides.lua"
        if not os.path.exists(f"{CLUSTER_DIR}/Master/modoverrides.lua"):
            write_modoverrides(read_modoverrides())
            created.append("modoverrides.lua")

        from app.services.mod_service import MODS_SETUP_PATH, default_mods_setup
        if not os.path.exists(MODS_SETUP_PATH):
            os.makedirs(os.path.dirname(MODS_SETUP_PATH), exist_ok=True)
            with open(MODS_SETUP_PATH, "w", encoding="utf-8") as f:
                f.write(default_mods_setup())
            created.append("mods/dedicated_server_mods_setup.lua")

        for fname in ["adminlist.txt", "blocklist.txt", "whitelist.txt"]:
            fpath = f"{CLUSTER_DIR}/{fname}"
            if not os.path.exists(fpath):
                open(fpath, "a").close()
                created.append(fname)

        return {"success": True, "created": created}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _apply_preset(mode: str, cluster_name: str = None, password: str = None) -> dict:
    preset = build_preset_config(mode, cluster_name, password if password is not None else "")
    name = preset["cluster"]["NETWORK.cluster_name"]
    try:
        os.makedirs(f"{CLUSTER_DIR}/Master", exist_ok=True)
        os.makedirs(f"{CLUSTER_DIR}/Caves", exist_ok=True)

        write_cluster_ini(preset["cluster"])
        write_shard_ini("Master", preset["master"])
        write_shard_ini("Caves", preset["caves"])
        ensure_worldgen_files()

        created = ["cluster.ini", "Master/server.ini", "Caves/server.ini"]

        mod_path = f"{CLUSTER_DIR}/modoverrides.lua"
        if not os.path.exists(f"{CLUSTER_DIR}/Master/modoverrides.lua"):
            write_modoverrides(read_modoverrides())
            created.append("modoverrides.lua")

        from app.services.mod_service import MODS_SETUP_PATH, default_mods_setup
        if not os.path.exists(MODS_SETUP_PATH):
            os.makedirs(os.path.dirname(MODS_SETUP_PATH), exist_ok=True)
            with open(MODS_SETUP_PATH, "w", encoding="utf-8") as f:
                f.write(default_mods_setup())
            created.append("mods/dedicated_server_mods_setup.lua")

        for fname in ["adminlist.txt", "blocklist.txt", "whitelist.txt"]:
            fpath = f"{CLUSTER_DIR}/{fname}"
            if not os.path.exists(fpath):
                open(fpath, "a").close()
                created.append(fname)

        offline = preset["cluster"]["NETWORK.offline_cluster"] == "true"
        ensure_shard_link_config()
        return {
            "success": True,
            "created": created,
            "preset": mode,
            "applied": {
                "cluster_name": name,
                "master_port": preset["master"]["NETWORK.server_port"],
                "caves_port": preset["caves"]["NETWORK.server_port"],
                "shard_port": preset["cluster"]["SHARD.master_port"],
                "offline": offline,
                "mode": mode,
            },
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def apply_friends_preset(cluster_name: str = None, password: str = None) -> dict:
    """Офлайн-пресет: без токена Klei, подключение по IP."""
    return _apply_preset("friends", cluster_name, password)


def apply_online_preset(cluster_name: str = None, password: str = None) -> dict:
    """Онлайн-пресет: сервер в браузере Klei, нужен cluster token."""
    return _apply_preset("online", cluster_name, password)


def get_cluster_binding_status() -> dict:
    from app.services.dst_service import get_shard_status

    cluster_path = f"{CLUSTER_DIR}/cluster.ini"
    cluster_exists = os.path.isfile(cluster_path)
    cluster = read_cluster_ini() if cluster_exists else {}
    master_ini = read_shard_ini("Master")
    caves_ini = read_shard_ini("Caves")
    master_status = get_shard_status("Master")

    master_shard_port = cluster.get("SHARD.master_port", "10888")
    master_ip = cluster.get("SHARD.master_ip", "127.0.0.1")
    cluster_key = cluster.get("SHARD.cluster_key", "default")
    bind_ip = cluster.get("SHARD.bind_ip", "0.0.0.0")
    shards_enabled = cluster_shards_enabled(cluster)
    offline = cluster.get("NETWORK.offline_cluster", "false").lower() == "true"
    token_ok = bool(read_cluster_token()) or offline

    master_game_port = master_ini.get("NETWORK.server_port", "10999")
    caves_game_port = caves_ini.get("NETWORK.server_port", "11000")
    caves_master_port = caves_ini.get("SHARD.master_port", "")
    caves_master_ip = caves_ini.get("SHARD.master_ip", "")

    master_synced = (
        cluster_exists
        and master_ini.get("SHARD.is_master", "").lower() == "true"
        and master_ini.get("SHARD.name", "") == "Master"
        and str(master_ini.get("SHARD.id", "")) == "1"
        and master_ip not in ("", "0.0.0.0")
        and shards_enabled
        and str(master_game_port) != str(caves_game_port)
    )

    caves_synced = (
        caves_ini.get("SHARD.is_master", "").lower() == "false"
        and caves_ini.get("SHARD.name", "") == "Caves"
        and str(caves_master_port) == str(master_shard_port)
        and str(caves_master_ip) == str(master_ip)
        and str(caves_ini.get("SHARD.id", "")) == "2"
        and str(caves_ini.get("SHARD.cluster_key", cluster_key)) == str(cluster_key)
    )

    return {
        "cluster": {
            "exists": cluster_exists,
            "name": cluster.get("NETWORK.cluster_name", ""),
            "master_ip": master_ip,
            "master_shard_port": master_shard_port,
            "cluster_key": cluster_key,
            "bind_ip": bind_ip,
            "shards_enabled": shards_enabled,
            "offline": offline,
            "token_ok": token_ok,
        },
        "master": {
            "running": master_status["running"],
            "pid": master_status.get("pid"),
            "game_port": master_game_port,
            "steam_master_port": master_ini.get("STEAM.master_server_port", "27016"),
            "steam_auth_port": master_ini.get("STEAM.authentication_port", "8766"),
            "synced": master_synced,
        },
        "caves": {
            "master_port": caves_master_port,
            "master_ip": caves_master_ip,
            "game_port": caves_game_port,
            "synced": caves_synced,
        },
        "ports_ok": str(master_game_port) != str(caves_game_port),
        "ready_to_start": (
            cluster_exists
            and shards_enabled
            and master_synced
            and caves_synced
            and str(master_game_port) != str(caves_game_port)
            and (offline or token_ok)
        ),
    }


def get_config_workflow_status() -> dict:
    binding = get_cluster_binding_status()
    cluster = binding["cluster"]
    steps = [
        {
            "id": "cluster",
            "label": "Настроить cluster.ini",
            "done": cluster.get("exists", False),
            "hint": "Вкладка «Кластер» → сохраните имя, пароль, shard_enabled=true",
        },
        {
            "id": "master_bind",
            "label": "Привязать Master к cluster.ini",
            "done": binding["master"].get("synced", False),
            "hint": "Вкладка «Master» → «Привязать к cluster.ini»",
        },
        {
            "id": "caves_bind",
            "label": "Привязать Caves к Master",
            "done": binding["caves"].get("synced", False),
            "hint": "Вкладка «Caves» → «Привязать к Master» (Master может быть остановлен)",
        },
    ]
    if not cluster.get("offline") and not cluster.get("token_ok"):
        steps.append({
            "id": "token",
            "label": "Сохранить Cluster Token",
            "done": False,
            "hint": "Вкладка «Токен» — для онлайн-сервера в браузере Klei",
        })
    ready = binding.get("ready_to_start", False)
    if not cluster.get("offline") and not cluster.get("token_ok"):
        ready = False
    return {
        "steps": steps,
        "ready_to_start": ready,
        "binding": binding,
    }


def validate_cluster_config() -> dict:
    """Проверка конфигов перед запуском кластера (без проверки процессов)."""
    binding = get_cluster_binding_status()
    cluster = binding["cluster"]
    errors = []
    hints = []

    if not cluster.get("exists"):
        errors.append("cluster.ini не найден")
        hints.append("Конфиг → Кластер: сохраните настройки или примените пресет")
    if not cluster.get("shards_enabled"):
        errors.append("shard_enabled выключен")
        hints.append("Конфиг → Кластер: включите «Шарды (пещеры)»")
    if not cluster.get("offline") and not cluster.get("token_ok"):
        errors.append("Нет Cluster Token")
        hints.append("Конфиг → Токен: сохраните токен Klei")
    if not binding["master"].get("synced"):
        errors.append("Master не привязан к cluster.ini")
        hints.append("Конфиг → Master → «Привязать к cluster.ini»")
    if not binding["caves"].get("synced"):
        errors.append("Caves не привязан к Master")
        hints.append("Конфиг → Caves → «Привязать к Master»")
    if not binding.get("ports_ok"):
        errors.append("Порты Master и Caves совпадают")
        hints.append("Повторите привязку Master и Caves — панель выставит 10999 / 11000")

    return {
        "ok": not errors,
        "errors": errors,
        "hints": hints,
        "binding": binding,
    }


def apply_cluster_bindings() -> dict:
    """Полная привязка конфигов: cluster.ini → Master → Caves."""
    repair = ensure_shard_link_config()
    ensure_worldgen_files()
    master = bind_master_to_cluster()
    if not master.get("success"):
        return {
            "success": False,
            "error": master.get("error", "Не удалось привязать Master"),
            "repair": repair,
            "master": master,
        }
    caves = bind_caves_to_master()
    if not caves.get("success"):
        return {
            "success": False,
            "error": caves.get("error", "Не удалось привязать Caves"),
            "repair": repair,
            "master": master,
            "caves": caves,
        }
    return {
        "success": True,
        "repair": repair,
        "master": master,
        "caves": caves,
        "binding": get_cluster_binding_status(),
    }


def get_caves_binding_status() -> dict:
    return get_cluster_binding_status()


def get_master_binding_status() -> dict:
    return get_cluster_binding_status()


def bind_master_to_cluster() -> dict:
    """Синхронизирует Master/server.ini и cluster.ini (параметры шардов)."""
    cluster_path = f"{CLUSTER_DIR}/cluster.ini"
    if not os.path.isfile(cluster_path):
        return {
            "success": False,
            "error": "cluster.ini не найден. Сначала примените пресет на вкладке «Запуск» или «Кластер».",
        }

    cluster = read_cluster_ini()
    cluster_updated = _ensure_cluster_shard_keys(cluster)

    master_shard_port = cluster["SHARD.master_port"]

    if cluster_updated:
        write_cluster_ini(cluster)

    master_existing = read_shard_ini("Master")
    caves_ini = read_shard_ini("Caves")
    master_game_port = master_existing.get("NETWORK.server_port") or "10999"
    caves_game_port = caves_ini.get("NETWORK.server_port", "11000")

    if str(master_game_port) == str(caves_game_port):
        master_game_port = "10999" if str(caves_game_port) != "10999" else "10998"

    master_data = dict(master_existing)
    template = _shard_ini_master()
    for key, value in template.items():
        master_data[key] = value
    for bad_key in ("SHARD.master_ip", "SHARD.master_port", "SHARD.cluster_key"):
        master_data.pop(bad_key, None)
    master_data["NETWORK.server_port"] = str(master_game_port)

    result = write_shard_ini("Master", master_data)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Не удалось записать Master/server.ini")}

    _sync_caves_shard_link(cluster)

    binding = get_cluster_binding_status()
    return {
        "success": True,
        "message": (
            f"Master привязан к cluster.ini "
            f"(игровой порт {master_game_port}, шарды {cluster['SHARD.master_ip']}:{master_shard_port})"
        ),
        "master": master_data,
        "binding": binding,
    }


def bind_caves_to_master() -> dict:
    """Синхронизирует Caves/server.ini с cluster.ini и Master/server.ini."""
    if not os.path.isfile(f"{CLUSTER_DIR}/cluster.ini"):
        return {
            "success": False,
            "error": "cluster.ini не найден. Сначала настройте вкладку «Кластер».",
        }

    binding = get_cluster_binding_status()

    if not binding["master"]["synced"]:
        return {
            "success": False,
            "error": "Master не привязан к cluster.ini. Сначала вкладка «Master» → «Привязать к cluster.ini».",
            "binding": binding,
        }

    cluster = read_cluster_ini()
    cluster_updated = _ensure_cluster_shard_keys(cluster)

    if cluster_updated:
        write_cluster_ini(cluster)

    master_shard_port = cluster.get("SHARD.master_port", "10888")
    master_ip = cluster.get("SHARD.master_ip", "127.0.0.1")
    cluster_key = cluster.get("SHARD.cluster_key", "default")

    caves_existing = read_shard_ini("Caves")
    caves_game_port = caves_existing.get("NETWORK.server_port") or "11000"
    master_game_port = binding["master"]["game_port"]

    if str(caves_game_port) == str(master_game_port):
        caves_game_port = "11000" if str(master_game_port) != "11000" else "11001"

    caves_data = dict(caves_existing)
    template = _shard_ini_caves(master_shard_port, master_ip, cluster_key)
    for key, value in template.items():
        caves_data[key] = value
    caves_data["NETWORK.server_port"] = str(caves_game_port)

    result = write_shard_ini("Caves", caves_data)
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Не удалось записать Caves/server.ini")}

    binding = get_cluster_binding_status()
    return {
        "success": True,
        "message": (
            f"Пещеры привязаны к Master ({master_ip}:{master_shard_port}, "
            f"игровой порт Caves: {caves_game_port})"
        ),
        "caves": caves_data,
        "binding": binding,
        "cluster_master_ip": master_ip,
    }
