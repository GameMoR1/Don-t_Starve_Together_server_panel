"""
Библиотека миров DST: именованные сейвы Master+Caves, выбор при запуске кластера.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.config.config_reader import (
    cluster_shards_enabled,
    get_cluster_dir,
    normalize_shard,
    read_cluster_ini,
    refresh_dst_paths,
    validate_cluster_config,
)

WORLD_LIBRARY_DIR = "/var/lib/dst-panel/world-library"
WORLDS_DIR = os.path.join(WORLD_LIBRARY_DIR, "worlds")
REGISTRY_PATH = os.path.join(WORLD_LIBRARY_DIR, "worlds.json")
ACTIVE_PATH = os.path.join(WORLD_LIBRARY_DIR, "active.json")

_SHARD_SAVE_ITEMS = (
    "session",
    "Save",
    "save",
    "saveindex",
    "SaveIndex",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _utcnow()).isoformat()


def _ensure_dirs() -> None:
    os.makedirs(WORLDS_DIR, exist_ok=True)
    try:
        os.chmod(WORLD_LIBRARY_DIR, 0o700)
    except OSError:
        pass


def _shard_dir(shard: str) -> str:
    shard = normalize_shard(shard)
    cluster = get_cluster_dir()
    return os.path.join(cluster, shard)


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


def _path_size(path: str) -> int:
    if not os.path.exists(path):
        return 0
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


def _load_json(path: str, default: Any) -> Any:
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return default


def _save_json(path: str, data: Any) -> None:
    _ensure_dirs()
    fd, tmp = tempfile.mkstemp(dir=WORLD_LIBRARY_DIR, suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _default_active() -> dict:
    return {"mode": "current", "world_id": None}


def get_active_selection() -> dict:
    _ensure_dirs()
    data = _load_json(ACTIVE_PATH, _default_active())
    if not isinstance(data, dict):
        return _default_active()
    mode = data.get("mode") or "current"
    if mode not in ("current", "library", "new"):
        mode = "current"
    return {"mode": mode, "world_id": data.get("world_id")}


def set_active_selection(*, mode: str, world_id: Optional[str] = None) -> dict:
    mode = (mode or "current").lower()
    if mode not in ("current", "library", "new"):
        return {"success": False, "error": "Некорректный режим: current, library или new"}
    if mode == "library":
        if not world_id:
            return {"success": False, "error": "Выберите мир из библиотеки"}
        world = get_world(world_id)
        if not world:
            return {"success": False, "error": "Мир не найден в библиотеке"}
    else:
        world_id = None
    active = {"mode": mode, "world_id": world_id, "updated_at": _iso()}
    _save_json(ACTIVE_PATH, active)
    return {"success": True, "active": active}


def _registry() -> dict:
    _ensure_dirs()
    data = _load_json(REGISTRY_PATH, {"worlds": []})
    if not isinstance(data, dict):
        return {"worlds": []}
    data.setdefault("worlds", [])
    return data


def _save_registry(data: dict) -> None:
    _save_json(REGISTRY_PATH, data)


def _world_dir(world_id: str) -> str:
    return os.path.join(WORLDS_DIR, world_id)


def _world_meta_path(world_id: str) -> str:
    return os.path.join(_world_dir(world_id), "meta.json")


def _scan_shard_save(shard: str) -> dict:
    shard = normalize_shard(shard)
    shard_dir = _shard_dir(shard)
    found: Dict[str, Any] = {
        "shard": shard,
        "path": shard_dir,
        "has_data": False,
        "size_bytes": 0,
        "session_ids": [],
        "items": [],
    }
    if not os.path.isdir(shard_dir):
        return found

    for item in _SHARD_SAVE_ITEMS:
        path = os.path.join(shard_dir, item)
        if not os.path.exists(path):
            continue
        size = _path_size(path)
        if size <= 0 and os.path.isdir(path):
            continue
        found["items"].append({"name": item, "path": path, "size_bytes": size})
        found["size_bytes"] += size
        found["has_data"] = True

    for root_name in ("session", os.path.join("Save", "session"), os.path.join("save", "session")):
        root = os.path.join(shard_dir, root_name)
        if not os.path.isdir(root):
            continue
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if os.path.isdir(folder) and name not in found["session_ids"]:
                found["session_ids"].append(name)

    return found


def scan_current_saves() -> dict:
    refresh_dst_paths()
    master = _scan_shard_save("Master")
    caves = _scan_shard_save("Caves")
    return {
        "master": master,
        "caves": caves,
        "has_any_data": master["has_data"] or caves["has_data"],
        "total_bytes": master["size_bytes"] + caves["size_bytes"],
    }


def _capture_shard_to_dir(shard: str, dest_shard_dir: str) -> dict:
    shard = normalize_shard(shard)
    src_dir = _shard_dir(shard)
    os.makedirs(dest_shard_dir, exist_ok=True)
    copied = []
    for item in _SHARD_SAVE_ITEMS:
        src = os.path.join(src_dir, item)
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest_shard_dir, item)
        if os.path.isdir(src):
            if os.path.exists(dst):
                _rmtree_safe(dst)
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
        copied.append(item)
    scan = _scan_shard_save(shard)
    return {
        "shard": shard,
        "copied": copied,
        "has_data": bool(copied),
        "size_bytes": scan["size_bytes"],
        "session_ids": scan["session_ids"],
    }


def _clear_shard_save(shard: str) -> List[str]:
    shard = normalize_shard(shard)
    shard_dir = _shard_dir(shard)
    removed = []
    for item in ("session", "Save", "save", "backup"):
        path = os.path.join(shard_dir, item)
        if os.path.exists(path):
            _rmtree_safe(path)
            removed.append(item)
    for name in ("saveindex", "SaveIndex"):
        path = os.path.join(shard_dir, name)
        if os.path.isfile(path):
            try:
                os.remove(path)
                removed.append(name)
            except OSError:
                pass
    return removed


def _apply_dir_to_shard(shard: str, src_shard_dir: str) -> dict:
    shard = normalize_shard(shard)
    if not os.path.isdir(src_shard_dir):
        return {"shard": shard, "applied": False, "reason": "no_library_data"}
    _clear_shard_save(shard)
    dest_dir = _shard_dir(shard)
    os.makedirs(dest_dir, exist_ok=True)
    applied = []
    for item in _SHARD_SAVE_ITEMS:
        src = os.path.join(src_shard_dir, item)
        if not os.path.exists(src):
            continue
        dst = os.path.join(dest_dir, item)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst)
        applied.append(item)
    return {"shard": shard, "applied": bool(applied), "items": applied}


def _build_world_record(
    world_id: str,
    name: str,
    *,
    description: str = "",
    master_info: Optional[dict] = None,
    caves_info: Optional[dict] = None,
    created_at: Optional[str] = None,
) -> dict:
    master_info = master_info or {}
    caves_info = caves_info or {}
    now = _iso()
    return {
        "id": world_id,
        "name": name,
        "description": description or "",
        "created_at": created_at or now,
        "updated_at": now,
        "master": {
            "has_data": bool(master_info.get("has_data")),
            "size_bytes": master_info.get("size_bytes", 0),
            "session_ids": master_info.get("session_ids", []),
        },
        "caves": {
            "has_data": bool(caves_info.get("has_data")),
            "size_bytes": caves_info.get("size_bytes", 0),
            "session_ids": caves_info.get("session_ids", []),
        },
        "total_bytes": (
            int(master_info.get("size_bytes", 0)) + int(caves_info.get("size_bytes", 0))
        ),
    }


def list_worlds() -> dict:
    _ensure_dirs()
    reg = _registry()
    worlds = []
    for entry in reg.get("worlds", []):
        wid = entry.get("id")
        if not wid:
            continue
        meta_path = _world_meta_path(wid)
        if os.path.isfile(meta_path):
            meta = _load_json(meta_path, entry)
            worlds.append(meta)
        else:
            worlds.append(entry)
    worlds.sort(key=lambda w: w.get("updated_at") or w.get("created_at") or "", reverse=True)
    active = get_active_selection()
    current = scan_current_saves()
    cluster = read_cluster_ini()
    shards_enabled = cluster_shards_enabled(cluster)
    return {
        "worlds": worlds,
        "active": active,
        "current_on_disk": current,
        "shards_enabled": shards_enabled,
    }


def get_world(world_id: str) -> Optional[dict]:
    if not world_id:
        return None
    meta_path = _world_meta_path(world_id)
    if os.path.isfile(meta_path):
        return _load_json(meta_path, None)
    reg = _registry()
    for entry in reg.get("worlds", []):
        if entry.get("id") == world_id:
            return entry
    return None


def get_world_readiness(world_id: Optional[str] = None) -> dict:
    refresh_dst_paths()
    cluster = read_cluster_ini()
    shards_enabled = cluster_shards_enabled(cluster)
    config_check = validate_cluster_config()
    active = get_active_selection()
    target_id = world_id or (active.get("world_id") if active.get("mode") == "library" else None)
    mode = active.get("mode") if world_id is None else "library"

    checks = [
        {
            "id": "cluster_config",
            "label": "Конфиг кластера",
            "ok": config_check.get("ok", False),
            "required": True,
            "hint": "; ".join(config_check.get("errors", [])) or "Готов",
        },
    ]

    world = None
    if mode == "library" and target_id:
        world = get_world(target_id)
        checks.append({
            "id": "world_exists",
            "label": "Мир в библиотеке",
            "ok": bool(world),
            "required": True,
            "hint": "Выберите существующий мир",
        })
        if world:
            master_ok = world.get("master", {}).get("has_data")
            caves_ok = world.get("caves", {}).get("has_data")
            checks.append({
                "id": "master_save",
                "label": "Сейв Master",
                "ok": bool(master_ok),
                "required": True,
                "hint": "В мире нет данных Master — импортируйте или сохраните текущий сейв",
            })
            if shards_enabled:
                checks.append({
                    "id": "caves_save",
                    "label": "Сейв Caves",
                    "ok": bool(caves_ok),
                    "required": False,
                    "hint": (
                        "Нет сейва Caves — пещеры сгенерируются заново при первом запуске"
                        if not caves_ok
                        else "Сейв Caves найден"
                    ),
                })
    elif mode == "new":
        checks.append({
            "id": "new_world",
            "label": "Новый мир",
            "ok": True,
            "required": False,
            "hint": "При запуске текущие сейвы будут очищены и создан новый мир",
        })
    else:
        current = scan_current_saves()
        checks.append({
            "id": "current_save",
            "label": "Сейв на диске",
            "ok": current.get("has_any_data", False),
            "required": False,
            "hint": (
                "На диске нет сейва — будет создан новый мир"
                if not current.get("has_any_data")
                else "Будет использован текущий сейв на диске"
            ),
        })

    required = [c for c in checks if c.get("required")]
    return {
        "mode": mode,
        "world_id": target_id,
        "world": world,
        "ready": all(c["ok"] for c in required),
        "checks": checks,
        "shards_enabled": shards_enabled,
    }


def create_world(
    name: str,
    *,
    description: str = "",
    from_current: bool = False,
    activate: bool = False,
) -> dict:
    name = (name or "").strip()
    if not name:
        return {"success": False, "error": "Укажите название мира"}
    if len(name) > 80:
        return {"success": False, "error": "Название слишком длинное (макс. 80 символов)"}

    _ensure_dirs()
    world_id = str(uuid.uuid4())
    world_path = _world_dir(world_id)
    os.makedirs(world_path, exist_ok=True)

    master_info = {"has_data": False, "size_bytes": 0, "session_ids": []}
    caves_info = {"has_data": False, "size_bytes": 0, "session_ids": []}

    if from_current:
        refresh_dst_paths()
        master_cap = _capture_shard_to_dir("Master", os.path.join(world_path, "Master"))
        caves_cap = _capture_shard_to_dir("Caves", os.path.join(world_path, "Caves"))
        master_info = {
            "has_data": master_cap.get("has_data", False),
            "size_bytes": _path_size(os.path.join(world_path, "Master")),
            "session_ids": master_cap.get("session_ids", []),
        }
        caves_info = {
            "has_data": caves_cap.get("has_data", False),
            "size_bytes": _path_size(os.path.join(world_path, "Caves")),
            "session_ids": caves_cap.get("session_ids", []),
        }

    meta = _build_world_record(
        world_id, name,
        description=description,
        master_info=master_info,
        caves_info=caves_info,
    )
    _save_json(_world_meta_path(world_id), meta)

    reg = _registry()
    reg["worlds"] = [w for w in reg.get("worlds", []) if w.get("id") != world_id]
    reg["worlds"].append(meta)
    _save_registry(reg)

    if activate:
        set_active_selection(mode="library", world_id=world_id)

    return {"success": True, "world": meta, "message": f"Мир «{name}» добавлен в библиотеку"}


def import_current_world(name: str, *, description: str = "", activate: bool = True) -> dict:
    current = scan_current_saves()
    if not current.get("has_any_data"):
        return {"success": False, "error": "На диске нет сейва для импорта"}
    return create_world(
        name,
        description=description,
        from_current=True,
        activate=activate,
    )


def update_world(world_id: str, *, name: Optional[str] = None, description: Optional[str] = None) -> dict:
    world = get_world(world_id)
    if not world:
        return {"success": False, "error": "Мир не найден"}
    if name is not None:
        name = name.strip()
        if not name:
            return {"success": False, "error": "Название не может быть пустым"}
        world["name"] = name
    if description is not None:
        world["description"] = description.strip()
    world["updated_at"] = _iso()
    _save_json(_world_meta_path(world_id), world)
    reg = _registry()
    reg["worlds"] = [
        world if w.get("id") == world_id else w
        for w in reg.get("worlds", [])
    ]
    _save_registry(reg)
    return {"success": True, "world": world}


def capture_world_from_current(world_id: str) -> dict:
    world = get_world(world_id)
    if not world:
        return {"success": False, "error": "Мир не найден"}
    current = scan_current_saves()
    if not current.get("has_any_data"):
        return {"success": False, "error": "На диске нет сейва для сохранения"}

    world_path = _world_dir(world_id)
    master_cap = _capture_shard_to_dir("Master", os.path.join(world_path, "Master"))
    caves_cap = _capture_shard_to_dir("Caves", os.path.join(world_path, "Caves"))
    world["master"] = {
        "has_data": master_cap.get("has_data", False),
        "size_bytes": _path_size(os.path.join(world_path, "Master")),
        "session_ids": master_cap.get("session_ids", []),
    }
    world["caves"] = {
        "has_data": caves_cap.get("has_data", False),
        "size_bytes": _path_size(os.path.join(world_path, "Caves")),
        "session_ids": caves_cap.get("session_ids", []),
    }
    world["total_bytes"] = world["master"]["size_bytes"] + world["caves"]["size_bytes"]
    world["updated_at"] = _iso()
    _save_json(_world_meta_path(world_id), world)
    reg = _registry()
    reg["worlds"] = [
        world if w.get("id") == world_id else w
        for w in reg.get("worlds", [])
    ]
    _save_registry(reg)
    return {
        "success": True,
        "world": world,
        "message": f"Текущий сейв сохранён в мир «{world['name']}»",
    }


def delete_world(world_id: str) -> dict:
    world = get_world(world_id)
    if not world:
        return {"success": False, "error": "Мир не найден"}
    active = get_active_selection()
    if active.get("world_id") == world_id:
        set_active_selection(mode="current", world_id=None)
    _rmtree_safe(_world_dir(world_id))
    reg = _registry()
    reg["worlds"] = [w for w in reg.get("worlds", []) if w.get("id") != world_id]
    _save_registry(reg)
    return {"success": True, "message": f"Мир «{world.get('name', world_id)}» удалён"}


def apply_world_to_cluster(world_id: str) -> dict:
    world = get_world(world_id)
    if not world:
        return {"success": False, "error": "Мир не найден"}
    if not world.get("master", {}).get("has_data"):
        return {"success": False, "error": "В мире нет данных Master"}

    refresh_dst_paths()
    world_path = _world_dir(world_id)
    master_res = _apply_dir_to_shard("Master", os.path.join(world_path, "Master"))
    caves_res = _apply_dir_to_shard("Caves", os.path.join(world_path, "Caves"))
    return {
        "success": True,
        "world_id": world_id,
        "world_name": world.get("name"),
        "master": master_res,
        "caves": caves_res,
        "message": f"Мир «{world.get('name')}» подготовлен к запуску",
    }


def clear_cluster_for_new_world() -> dict:
    refresh_dst_paths()
    cleared = {}
    for shard in ("Master", "Caves"):
        cleared[shard] = _clear_shard_save(shard)
    return {"success": True, "cleared": cleared, "message": "Сейвы очищены для нового мира"}


def prepare_active_world_for_cluster_start() -> dict:
    """Применить выбранный мир перед запуском кластера (шарды должны быть остановлены)."""
    refresh_dst_paths()
    active = get_active_selection()
    mode = active.get("mode", "current")

    if mode == "library":
        world_id = active.get("world_id")
        if not world_id:
            return {"success": False, "error": "Не выбран мир из библиотеки"}
        readiness = get_world_readiness(world_id)
        if not readiness.get("ready"):
            failed = [c for c in readiness.get("checks", []) if c.get("required") and not c.get("ok")]
            hints = "; ".join(c.get("hint", c.get("label", "")) for c in failed)
            return {"success": False, "error": f"Мир не готов: {hints}", "checks": readiness.get("checks")}
        result = apply_world_to_cluster(world_id)
        result["mode"] = mode
        return result

    if mode == "new":
        result = clear_cluster_for_new_world()
        result["mode"] = mode
        return result

    return {
        "success": True,
        "mode": "current",
        "message": "Используется текущий сейв на диске",
    }


def get_worlds_summary() -> dict:
    data = list_worlds()
    return {
        "count": len(data.get("worlds", [])),
        "active": data.get("active"),
        "worlds": [
            {
                "id": w.get("id"),
                "name": w.get("name"),
                "total_bytes": w.get("total_bytes", 0),
            }
            for w in data.get("worlds", [])
        ],
    }


def export_for_backup(staging_dir: str) -> Optional[str]:
    _ensure_dirs()
    if not os.path.isdir(WORLD_LIBRARY_DIR):
        return None
    dest = os.path.join(staging_dir, "panel", "world-library")
    if os.path.exists(dest):
        _rmtree_safe(dest)
    shutil.copytree(WORLD_LIBRARY_DIR, dest, symlinks=True)
    return dest


def import_from_backup(extracted_dir: str) -> dict:
    src = os.path.join(extracted_dir, "panel", "world-library")
    if not os.path.isdir(src):
        src = os.path.join(extracted_dir, "world-library")
    if not os.path.isdir(src):
        return {"imported": False}
    if os.path.isdir(WORLD_LIBRARY_DIR):
        _rmtree_safe(WORLD_LIBRARY_DIR)
    shutil.copytree(src, WORLD_LIBRARY_DIR, symlinks=True)
    _ensure_dirs()
    return {"imported": True, "path": WORLD_LIBRARY_DIR}
