import os
import re
import json
import urllib.request
import urllib.parse
import urllib.error
from typing import List, Optional

from app.services.dst_service import DST_DIR, CLUSTER_DIR

DST_WORKSHOP_APPID = "322330"
MODS_SETUP_PATH = f"{DST_DIR}/mods/dedicated_server_mods_setup.lua"
MODOVERRIDES_PATHS = [
    f"{CLUSTER_DIR}/Master/modoverrides.lua",
    f"{CLUSTER_DIR}/Caves/modoverrides.lua",
    f"{CLUSTER_DIR}/modoverrides.lua",
]

_SETUP_MOD_RE = re.compile(r'ServerModSetup\s*\(\s*["\'](\d+)["\']\s*\)')
_SETUP_COLLECTION_RE = re.compile(
    r'ServerModCollectionSetup\s*\(\s*["\'](\d+)["\']\s*\)'
)
_OVERRIDE_MOD_RE = re.compile(r'\["workshop-(\d+)"\]')


def _default_modoverrides() -> str:
    return "return {\n}\n"


def default_mods_setup() -> str:
    return "-- Автогенерация DST Panel\n-- ServerModSetup(\"WORKSHOP_ID\")\n"


def _default_mods_setup() -> str:
    return default_mods_setup()


def read_mods_setup() -> str:
    if not os.path.exists(MODS_SETUP_PATH):
        return _default_mods_setup()
    try:
        with open(MODS_SETUP_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return _default_mods_setup()


def parse_mods_setup(content: str) -> dict:
    return {
        "mods": _SETUP_MOD_RE.findall(content or ""),
        "collections": _SETUP_COLLECTION_RE.findall(content or ""),
    }


def write_mods_setup(mod_ids: List[str], collection_ids: Optional[List[str]] = None) -> dict:
    collection_ids = collection_ids or []
    lines = [
        "-- Автогенерация DST Panel",
        "-- Скачивание при старте Master/Caves (Klei + Steam Workshop)",
        "",
    ]
    for cid in collection_ids:
        lines.append(f'ServerModCollectionSetup("{cid}")')
    for mid in sorted(set(mod_ids), key=int):
        lines.append(f'ServerModSetup("{mid}")')
    lines.append("")
    try:
        os.makedirs(os.path.dirname(MODS_SETUP_PATH), exist_ok=True)
        with open(MODS_SETUP_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return {"success": True, "path": MODS_SETUP_PATH}
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_modoverrides() -> str:
    for path in MODOVERRIDES_PATHS:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                continue
    return _default_modoverrides()


def parse_modoverrides_ids(content: str) -> List[str]:
    return list(dict.fromkeys(_OVERRIDE_MOD_RE.findall(content or "")))


def build_modoverrides(mod_ids: List[str], existing_content: str = "") -> str:
    disabled = set()
    for mid in parse_modoverrides_ids(existing_content):
        block = re.search(
            rf'\["workshop-{mid}"\]\s*=\s*\{{[^}}]*enabled\s*=\s*false',
            existing_content,
            re.DOTALL,
        )
        if block:
            disabled.add(mid)

    lines = ["return {"]
    for mid in sorted(set(mod_ids), key=int):
        if mid in disabled:
            lines.append(f'  ["workshop-{mid}"] = {{ enabled = false }},')
        else:
            lines.append(f'  ["workshop-{mid}"] = {{ enabled = true }},')
    lines.append("}")
    lines.append("")
    return "\n".join(lines)


def write_modoverrides(content: str) -> dict:
    try:
        os.makedirs(f"{CLUSTER_DIR}/Master", exist_ok=True)
        os.makedirs(f"{CLUSTER_DIR}/Caves", exist_ok=True)
        for path in MODOVERRIDES_PATHS:
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        return {"success": True, "paths": MODOVERRIDES_PATHS}
    except Exception as e:
        return {"success": False, "error": str(e)}


def is_mod_downloaded(workshop_id: str) -> bool:
    candidates = [
        f"{DST_DIR}/mods/workshop-{workshop_id}",
        f"{DST_DIR}/steamapps/workshop/content/{DST_WORKSHOP_APPID}/{workshop_id}",
    ]
    return any(os.path.isdir(p) for p in candidates)


def _steam_api_post(endpoint: str, fields: dict) -> Optional[dict]:
    key = os.environ.get("STEAM_WEB_API_KEY", "").strip()
    if key:
        fields = {**fields, "key": key}
    data = urllib.parse.urlencode(fields).encode("utf-8")
    url = f"https://api.steampowered.com/{endpoint}"
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def fetch_workshop_titles(mod_ids: List[str]) -> dict:
    if not mod_ids:
        return {}
    fields = {"itemcount": len(mod_ids), "appid": DST_WORKSHOP_APPID}
    for i, mid in enumerate(mod_ids):
        fields[f"publishedfileids[{i}]"] = mid
    result = _steam_api_post("ISteamRemoteStorage/GetPublishedFileDetails/v1/", fields)
    titles = {}
    if result and result.get("response", {}).get("result") == 1:
        for item in result["response"].get("publishedfiledetails", []):
            titles[str(item.get("publishedfileid", ""))] = item.get("title", "")
    return titles


def fetch_collection_mod_ids(collection_id: str) -> dict:
    fields = {"collectioncount": 1, "publishedfileids[0]": collection_id}
    result = _steam_api_post("ISteamRemoteStorage/GetCollectionDetails/v1/", fields)
    if not result:
        return {
            "success": False,
            "error": (
                "Не удалось получить коллекцию Steam. "
                "Добавьте STEAM_WEB_API_KEY в .env панели (https://steamcommunity.com/dev/apikey) "
                "или добавьте моды по одному."
            ),
        }
    response = result.get("response", {})
    if response.get("result") != 1:
        return {
            "success": False,
            "error": response.get("resultmessage", "Коллекция не найдена"),
        }
    children = []
    for detail in response.get("collectiondetails", []):
        for child in detail.get("children", []):
            cid = str(child.get("publishedfileid", ""))
            if cid.isdigit():
                children.append(cid)
    if not children:
        return {"success": False, "error": "Коллекция пуста или недоступна"}
    return {"success": True, "mod_ids": children, "collection_id": collection_id}


def get_mods_overview() -> dict:
    setup_content = read_mods_setup()
    setup = parse_mods_setup(setup_content)
    overrides_content = read_modoverrides()
    override_ids = parse_modoverrides_ids(overrides_content)

    all_ids = list(dict.fromkeys(setup["mods"] + override_ids))
    titles = fetch_workshop_titles(all_ids)

    mods = []
    for mid in all_ids:
        in_setup = mid in setup["mods"]
        in_overrides = mid in override_ids
        mods.append({
            "workshop_id": mid,
            "title": titles.get(mid) or f"workshop-{mid}",
            "in_setup": in_setup,
            "in_overrides": in_overrides,
            "downloaded": is_mod_downloaded(mid),
            "enabled": in_overrides,
        })

    return {
        "mods": mods,
        "collections": setup["collections"],
        "setup_path": MODS_SETUP_PATH,
        "overrides_paths": MODOVERRIDES_PATHS[:2],
        "setup_content": setup_content,
        "overrides_content": overrides_content,
        "steam_api_configured": bool(os.environ.get("STEAM_WEB_API_KEY", "").strip()),
        "note": (
            "DST скачивает моды из dedicated_server_mods_setup.lua при старте шардов. "
            "modoverrides.lua включает моды на Master и Caves. "
            "Список подписок из Steam-клиента напрямую недоступен — используйте ID модов или коллекцию Workshop."
        ),
    }


def _collect_all_mod_ids() -> tuple:
    setup = parse_mods_setup(read_mods_setup())
    override_ids = parse_modoverrides_ids(read_modoverrides())
    mod_ids = list(dict.fromkeys(setup["mods"] + override_ids))
    return mod_ids, setup["collections"]


def add_mod(workshop_id: str) -> dict:
    wid = str(workshop_id).strip()
    if not wid.isdigit():
        return {"success": False, "error": "Некорректный Workshop ID"}

    mod_ids, collections = _collect_all_mod_ids()
    if wid not in mod_ids:
        mod_ids.append(wid)

    setup_result = write_mods_setup(mod_ids, collections)
    if not setup_result.get("success"):
        return setup_result

    overrides = build_modoverrides(mod_ids, read_modoverrides())
    override_result = write_modoverrides(overrides)
    if not override_result.get("success"):
        return override_result

    titles = fetch_workshop_titles([wid])
    return {
        "success": True,
        "workshop_id": wid,
        "title": titles.get(wid, ""),
        "message": "Мод добавлен в setup и modoverrides. Перезапустите шарды для скачивания.",
        "overview": get_mods_overview(),
    }


def add_collection(collection_id: str) -> dict:
    cid = str(collection_id).strip()
    if not cid.isdigit():
        return {"success": False, "error": "Некорректный ID коллекции"}

    fetched = fetch_collection_mod_ids(cid)
    if not fetched.get("success"):
        return fetched

    mod_ids, collections = _collect_all_mod_ids()
    if cid not in collections:
        collections.append(cid)
    for mid in fetched["mod_ids"]:
        if mid not in mod_ids:
            mod_ids.append(mid)

    setup_result = write_mods_setup(mod_ids, collections)
    if not setup_result.get("success"):
        return setup_result

    overrides = build_modoverrides(mod_ids, read_modoverrides())
    override_result = write_modoverrides(overrides)
    if not override_result.get("success"):
        return override_result

    return {
        "success": True,
        "collection_id": cid,
        "added_mods": fetched["mod_ids"],
        "message": (
            f"Коллекция {cid}: добавлено {len(fetched['mod_ids'])} модов. "
            "Перезапустите шарды для скачивания."
        ),
        "overview": get_mods_overview(),
    }


def remove_mod(workshop_id: str) -> dict:
    wid = str(workshop_id).strip()
    mod_ids, collections = _collect_all_mod_ids()
    mod_ids = [m for m in mod_ids if m != wid]

    setup_result = write_mods_setup(mod_ids, collections)
    if not setup_result.get("success"):
        return setup_result

    overrides = build_modoverrides(mod_ids, read_modoverrides())
    override_result = write_modoverrides(overrides)
    if not override_result.get("success"):
        return override_result

    return {
        "success": True,
        "message": f"Мод {wid} удалён из конфигурации",
        "overview": get_mods_overview(),
    }


def sync_mod_files_from_overrides(content: str) -> dict:
    mod_ids = parse_modoverrides_ids(content)
    setup = parse_mods_setup(read_mods_setup())
    merged = list(dict.fromkeys(mod_ids + setup["mods"]))

    override_result = write_modoverrides(content)
    if not override_result.get("success"):
        return override_result

    setup_result = write_mods_setup(merged, setup["collections"])
    if not setup_result.get("success"):
        return setup_result

    return {
        "success": True,
        "message": "modoverrides синхронизирован на Master/Caves, setup обновлён",
        "overview": get_mods_overview(),
    }
