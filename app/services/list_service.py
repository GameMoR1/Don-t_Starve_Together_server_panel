import os
import re
from typing import Dict, List, Optional

from app.config.config_reader import CLUSTER_DIR, read_text_file, write_text_file

LIST_FILES = {
    "admin": "adminlist.txt",
    "block": "blocklist.txt",
    "whitelist": "whitelist.txt",
}

KLEI_ID_RE = re.compile(r"^(?:KU_|OU_)[A-Za-z0-9_]+$", re.IGNORECASE)


def _list_path(list_type: str) -> str:
    filename = LIST_FILES.get(list_type)
    if not filename:
        raise ValueError(f"Unknown list type: {list_type}")
    return f"{CLUSTER_DIR}/{filename}"


def parse_list_content(content: str) -> List[str]:
    ids = []
    seen = set()
    for raw in (content or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        kid = line.split("#", 1)[0].strip()
        if KLEI_ID_RE.match(kid) and kid not in seen:
            seen.add(kid)
            ids.append(kid)
    return ids


def read_list(list_type: str) -> List[str]:
    path = _list_path(list_type)
    content = read_text_file(path)
    if content is None:
        return []
    return parse_list_content(content)


def write_list(list_type: str, ids: List[str]) -> dict:
    valid = []
    seen = set()
    for kid in ids:
        kid = kid.strip()
        if not KLEI_ID_RE.match(kid):
            continue
        if kid not in seen:
            seen.add(kid)
            valid.append(kid)
    path = _list_path(list_type)
    content = "\n".join(valid)
    if content:
        content += "\n"
    return write_text_file(path, content)


def read_all_lists() -> dict:
    admin = read_list("admin")
    block = read_list("block")
    whitelist = read_list("whitelist")
    return {
        "admin": admin,
        "block": block,
        "whitelist": whitelist,
        "files": LIST_FILES,
        "note": (
            "Списки читаются DST при старте шардов. "
            "После изменений перезапустите Master/Caves, чтобы применить бан/админку."
        ),
    }


def get_player_roles(klei_id: str) -> dict:
    return {
        "admin": klei_id in read_list("admin"),
        "banned": klei_id in read_list("block"),
        "whitelisted": klei_id in read_list("whitelist"),
    }


def add_to_list(list_type: str, klei_id: str) -> dict:
    kid = klei_id.strip()
    if not KLEI_ID_RE.match(kid):
        return {"success": False, "error": "Некорректный Klei ID (ожидается KU_... или OU_...)"}
    ids = read_list(list_type)
    if kid not in ids:
        ids.append(kid)
    result = write_list(list_type, ids)
    if not result.get("success"):
        return result
    from app.services.player_service import invalidate_players_cache
    invalidate_players_cache()
    return {"success": True, "list": list_type, "klei_id": kid, "lists": read_all_lists()}


def remove_from_list(list_type: str, klei_id: str) -> dict:
    kid = klei_id.strip()
    ids = [i for i in read_list(list_type) if i != kid]
    result = write_list(list_type, ids)
    if not result.get("success"):
        return result
    from app.services.player_service import invalidate_players_cache
    invalidate_players_cache()
    return {"success": True, "list": list_type, "klei_id": kid, "lists": read_all_lists()}


def apply_list_action(klei_id: str, action: str) -> dict:
    kid = klei_id.strip()
    if not KLEI_ID_RE.match(kid):
        return {"success": False, "error": "Некорректный Klei ID"}

    actions = {
        "add_admin": ("admin", True, ["block"]),
        "remove_admin": ("admin", False, []),
        "add_ban": ("block", True, ["admin", "whitelist"]),
        "remove_ban": ("block", False, []),
        "add_whitelist": ("whitelist", True, ["block"]),
        "remove_whitelist": ("whitelist", False, []),
    }
    spec = actions.get(action)
    if not spec:
        return {"success": False, "error": f"Неизвестное действие: {action}"}

    list_type, add, remove_from = spec
    if add:
        for other in remove_from:
            remove_from_list(other, kid)
        result = add_to_list(list_type, kid)
    else:
        result = remove_from_list(list_type, kid)

    if not result.get("success"):
        return result

    from app.services.player_service import invalidate_players_cache
    invalidate_players_cache()

    labels = {
        "add_admin": "выдана админка",
        "remove_admin": "админка снята",
        "add_ban": "игрок забанен",
        "remove_ban": "игрок разбанен",
        "add_whitelist": "добавлен в белый список",
        "remove_whitelist": "удалён из белого списка",
    }
    return {
        "success": True,
        "action": action,
        "klei_id": kid,
        "message": f"{kid}: {labels[action]}. Перезапустите шарды для применения.",
        "roles": get_player_roles(kid),
        "lists": read_all_lists(),
    }
