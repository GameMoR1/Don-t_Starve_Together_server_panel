import os
import subprocess
from typing import List

from app.services.dst_service import DST_DIR, CLUSTER_DIR, BIN_DIR, libcurl_gnutls_ok
from app.config.config_reader import (
    read_cluster_ini, read_shard_ini, read_cluster_token,
    apply_friends_preset, apply_online_preset,
    get_cluster_binding_status, ensure_shard_link_config, KLEI_DIR,
    cluster_shards_enabled,
)

DST_BINARY = f"{BIN_DIR}/dontstarve_dedicated_server_nullrenderer"


def get_server_ips() -> List[str]:
    try:
        result = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=5
        )
        return [ip for ip in result.stdout.strip().split() if ip]
    except Exception:
        return []


def _launch_steps(
    *,
    steamcmd_ok: bool,
    binary_ok: bool,
    cluster_ini_ok: bool,
    shard_link_ok: bool,
    token_ok: bool,
    offline: bool,
    master_running: bool,
    caves_running: bool,
    shards_enabled: bool,
) -> list:
    dst_ok = steamcmd_ok and binary_ok
    config_ok = cluster_ini_ok and shard_link_ok
    token_step_ok = token_ok or offline

    steps = [
        {
            "id": "install",
            "label": "Установить DST",
            "description": "SteamCMD и файлы dedicated-сервера",
            "ok": dst_ok,
            "required": True,
            "action": "server",
            "action_label": "Сервер → Установить DST",
        },
        {
            "id": "preset",
            "label": "Создать конфиг",
            "description": "cluster.ini + Master + Caves с привязкой шардов",
            "ok": config_ok,
            "required": True,
            "action": "setup",
            "action_label": "Выберите пресет выше",
        },
        {
            "id": "token",
            "label": "Cluster Token",
            "description": "Только для онлайн-сервера в браузере Klei",
            "ok": token_step_ok,
            "required": not offline,
            "action": "setup",
            "action_label": "Сохраните токен ниже",
        },
        {
            "id": "master",
            "label": "Запустить Master",
            "description": "Поверхность — единственный шард в списке Klei",
            "ok": master_running,
            "required": True,
            "action": "server",
            "action_label": "Сервер → Старт Master",
        },
    ]

    if shards_enabled:
        steps.append({
            "id": "caves",
            "label": "Запустить Caves",
            "description": "Пещеры подключаются к Master (не отдельный сервер в браузере)",
            "ok": caves_running,
            "required": True,
            "action": "server",
            "action_label": "Сервер → Старт Caves",
        })

    return steps


def get_setup_status(master_running: bool = False, caves_running: bool = False) -> dict:
    cluster = read_cluster_ini()
    master = read_shard_ini("Master")
    caves = read_shard_ini("Caves")
    token = read_cluster_token()
    binding = get_cluster_binding_status()
    offline = cluster.get("NETWORK.offline_cluster", "false").lower() == "true"
    lan_only = cluster.get("NETWORK.lan_only_cluster", "false").lower() == "true"
    shards_enabled = cluster_shards_enabled(cluster)

    master_port = master.get("NETWORK.server_port", "10999")
    caves_port = caves.get("NETWORK.server_port", "11000")
    master_shard_port = cluster.get("SHARD.master_port", "10888")
    master_ip = cluster.get("SHARD.master_ip", "127.0.0.1")
    caves_master_ip = caves.get("SHARD.master_ip", "")
    cluster_name = cluster.get("NETWORK.cluster_name", "My DST Server")
    cluster_password = cluster.get("NETWORK.cluster_password", "").strip()
    has_password = bool(cluster_password)
    bind_ip = cluster.get("SHARD.bind_ip", "0.0.0.0")

    binary_ok = os.path.exists(DST_BINARY)
    cluster_ini_ok = os.path.exists(f"{CLUSTER_DIR}/cluster.ini")
    master_ini_ok = os.path.exists(f"{CLUSTER_DIR}/Master/server.ini")
    caves_ini_ok = os.path.exists(f"{CLUSTER_DIR}/Caves/server.ini")
    token_ok = bool(token) or offline
    steamcmd_ok = os.path.exists(f"{DST_DIR}/steamcmd/steamcmd.sh")
    libcurl_ok = libcurl_gnutls_ok()

    shard_link_ok = (
        binding["master"]["synced"]
        and binding["caves"]["synced"]
        and binding["ports_ok"]
    )

    checks = [
        {
            "id": "steamcmd",
            "label": "SteamCMD установлен",
            "ok": steamcmd_ok,
            "required": True,
            "hint": "Запустите install.sh на сервере или «Установить DST» в панели",
        },
        {
            "id": "binary",
            "label": "Файлы DST-сервера",
            "ok": binary_ok,
            "required": True,
            "hint": "Скачайте сервер через SteamCMD (кнопка «Установить DST»)",
        },
        {
            "id": "libcurl_gnutls",
            "label": "libcurl-gnutls (32-bit)",
            "ok": libcurl_ok,
            "required": True,
            "hint": "sudo apt install libcurl3-gnutls:i386 && sudo bash scripts/install.sh",
        },
        {
            "id": "cluster_ini",
            "label": "cluster.ini",
            "ok": cluster_ini_ok,
            "required": True,
            "hint": "Выберите пресет «Онлайн» или «Офлайн» на этой странице",
        },
        {
            "id": "master_ini",
            "label": "Master/server.ini (is_master=true)",
            "ok": master_ini_ok and binding["master"]["synced"],
            "required": True,
            "hint": "Примените пресет или Конфиг → Master → «Привязать к cluster.ini»",
        },
        {
            "id": "caves_ini",
            "label": "Caves/server.ini (is_master=false)",
            "ok": caves_ini_ok and binding["caves"]["synced"],
            "required": shards_enabled,
            "hint": "Пресет задаёт master_ip; иначе Конфиг → Caves → «Привязать к Master»",
        },
        {
            "id": "shard_link",
            "label": "Привязка Master ↔ Caves",
            "ok": shard_link_ok,
            "required": shards_enabled and cluster_ini_ok,
            "hint": "Без master_ip в Caves в браузере Klei будет два одинаковых сервера",
        },
        {
            "id": "token",
            "label": "Cluster Token (Klei)",
            "ok": token_ok,
            "required": not offline,
            "hint": "Получите на https://accounts.klei.com/account/game/servers",
        },
    ]

    required_checks = [c for c in checks if c["required"]]
    ready = all(c["ok"] for c in required_checks)

    launch_steps = _launch_steps(
        steamcmd_ok=steamcmd_ok,
        binary_ok=binary_ok,
        cluster_ini_ok=cluster_ini_ok,
        shard_link_ok=shard_link_ok,
        token_ok=token_ok,
        offline=offline,
        master_running=master_running,
        caves_running=caves_running,
        shards_enabled=shards_enabled,
    )
    launch_ready = all(s["ok"] for s in launch_steps if s["required"])

    server_ips = get_server_ips()
    primary_ip = server_ips[0] if server_ips else "YOUR_SERVER_IP"

    def _c_connect(ip: str, port: str) -> str:
        if has_password:
            return f'c_connect("{ip}", {port}, "{cluster_password}")'
        return f'c_connect("{ip}", {port})'

    firewall_udp = [master_port]
    if shards_enabled:
        firewall_udp.extend([caves_port, master_shard_port])

    if offline:
        friend_hint = (
            "Офлайн-кластер: токен Klei не нужен. Игроки подключаются к Master по IP "
            "(команда c_connect). Пещеры подгружаются автоматически при спуске."
        )
        klei_hint = None
    else:
        friend_hint = (
            "Онлайн-кластер: в браузере игры — одна строка с названием кластера. "
            "Caves не регистрируется отдельно (is_master=false, master_ip в server.ini). "
            "Сначала Master, затем Caves."
        )
        klei_hint = (
            "Если видите два одинаковых сервера — остановите шарды, "
            "нажмите «Проверить привязку» и запустите Master → Caves заново."
        )

    connection = {
        "server_ips": server_ips,
        "primary_ip": primary_ip,
        "cluster_name": cluster_name,
        "master_port": master_port,
        "caves_port": caves_port,
        "master_shard_port": master_shard_port,
        "master_ip": master_ip,
        "caves_master_ip": caves_master_ip,
        "bind_ip": bind_ip,
        "offline": offline,
        "lan_only": lan_only,
        "shards_enabled": shards_enabled,
        "has_password": has_password,
        "has_token": bool(token),
        "firewall_udp_ports": firewall_udp,
        "direct_connect_master": _c_connect(primary_ip, master_port),
        "direct_connect_caves": _c_connect(primary_ip, caves_port) if shards_enabled else None,
        "steam_search": cluster_name if not offline and token_ok else None,
        "friend_hint": friend_hint,
        "klei_duplicate_hint": klei_hint,
        "mode_label": (
            "Офлайн (LAN)" if offline and lan_only
            else "Офлайн" if offline
            else "Онлайн (Klei)"
        ),
    }

    return {
        "ready": ready,
        "launch_ready": launch_ready,
        "checks": checks,
        "launch_steps": launch_steps,
        "binding": binding,
        "connection": connection,
        "paths": {
            "dst_dir": DST_DIR,
            "klei_dir": KLEI_DIR,
            "cluster_dir": CLUSTER_DIR,
            "token_file": f"{CLUSTER_DIR}/cluster_token.txt",
        },
    }


def repair_shard_link() -> dict:
    result = ensure_shard_link_config()
    binding = get_cluster_binding_status()
    return {
        "success": True,
        "changed": result.get("changed", False),
        "items": result.get("items", []),
        "binding": binding,
        "message": (
            "Привязка шардов исправлена: " + ", ".join(result.get("items", []))
            if result.get("changed")
            else "Привязка шардов уже корректна"
        ),
    }


def init_cluster(cluster_name: str = None, password: str = None) -> dict:
    return apply_online_preset(cluster_name, password)


def init_friends_cluster(cluster_name: str = None, password: str = None) -> dict:
    return apply_friends_preset(cluster_name, password)


def init_online_cluster(cluster_name: str = None, password: str = None) -> dict:
    return apply_online_preset(cluster_name, password)
