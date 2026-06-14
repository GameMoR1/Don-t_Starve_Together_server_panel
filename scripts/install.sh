#!/usr/bin/env bash
set -euo pipefail

DST_USER="dstpanel"
DST_DIR="/home/${DST_USER}/dst"
KLEI_DIR="${DST_DIR}/DoNotStarveTogether"
PANEL_DIR="/opt/dst-panel"
STEAMCMD_DIR="${DST_DIR}/steamcmd"
CLUSTER_DIR="${KLEI_DIR}/cluster"
MASTER_DIR="${CLUSTER_DIR}/Master"
CAVES_DIR="${CLUSTER_DIR}/Caves"
BACKUP_DIR="/var/lib/dst-panel/backups"
TOKEN_BACKUP="/var/lib/dst-panel/.cluster_token.backup"
DST_BINARY="${DST_DIR}/bin/dontstarve_dedicated_server_nullrenderer"
PANEL_PORT="${PANEL_PORT:-8000}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

install_ubuntu_deps() {
    apt-get update -qq

    local base_pkgs=(
        python3 python3-pip python3-venv curl wget tar gzip unzip git sqlite3
    )
    apt-get install -y -qq "${base_pkgs[@]}"

    if [[ "$(dpkg --print-architecture)" != "amd64" ]]; then
        return 0
    fi

    if ! dpkg --print-foreign-architectures | grep -qw i386; then
        log_info "Enabling i386 architecture for SteamCMD/DST..."
        dpkg --add-architecture i386
        apt-get update -qq
    fi

    local i386_pkgs=(lib32gcc-s1 lib32stdc++6 libstdc++6:i386)

    if apt-cache show libgcc-s1:i386 &>/dev/null; then
        i386_pkgs+=(libgcc-s1:i386)
    elif apt-cache show libgcc1:i386 &>/dev/null; then
        i386_pkgs+=(libgcc1:i386)
    fi

    if apt-cache show zlib1g:i386 &>/dev/null; then
        i386_pkgs+=(zlib1g:i386)
    fi

    # DST binary требует libcurl-gnutls.so.4 (не обычный libcurl4)
    local curl_pkg=""
    for candidate in \
        libcurl3-gnutls:i386 \
        libcurl4-gnutls-dev:i386 \
        libcurl3t64-gnutls:i386; do
        if apt-cache show "$candidate" &>/dev/null; then
            curl_pkg="$candidate"
            i386_pkgs+=("$candidate")
            break
        fi
    done
    if [[ -z "$curl_pkg" ]]; then
        for candidate in libcurl4t64:i386 libcurl4:i386; do
            if apt-cache show "$candidate" &>/dev/null; then
                i386_pkgs+=("$candidate")
                break
            fi
        done
    fi

    apt-get install -y -qq "${i386_pkgs[@]}"
    verify_libcurl_gnutls_i386
}

verify_libcurl_gnutls_i386() {
    local lib_dir="/usr/lib/i386-linux-gnu"
    local target="${lib_dir}/libcurl-gnutls.so.4"

    if [[ -f "$target" || -L "$target" ]]; then
        log_info "libcurl-gnutls.so.4: OK"
        return 0
    fi

    local libcurl=""
    for candidate in "${lib_dir}/libcurl-gnutls.so.3" "${lib_dir}/libcurl.so.4" "${lib_dir}/libcurl.so.3"; do
        if [[ -f "$candidate" ]]; then
            libcurl="$candidate"
            break
        fi
    done

    if [[ -n "$libcurl" ]]; then
        log_warn "libcurl-gnutls.so.4 не найден — создаём symlink → ${libcurl}"
        ln -sf "$libcurl" "$target"
        ldconfig 2>/dev/null || true
        return 0
    fi

    log_error "Не найден libcurl-gnutls.so.4. Установите вручную:"
    log_error "  apt install libcurl3-gnutls:i386"
    log_error "  или: apt install libcurl4-gnutls-dev:i386"
    return 1
}

migrate_cluster_layout() {
    local old="${DST_DIR}/cluster"
    local new="${CLUSTER_DIR}"

    mkdir -p "${KLEI_DIR}"

    if [[ -d "${old}" && ! -e "${new}" ]]; then
        log_info "Перенос кластера: ${old} → ${new}"
        mv "${old}" "${new}"
    elif [[ -d "${old}" && -d "${new}" ]]; then
        log_warn "Найдены оба каталога cluster (старый и DoNotStarveTogether/cluster)"
        log_warn "Используется: ${new}"
    fi
}

write_panel_env() {
    local secret=""
    local steam_key=""
    if [[ -f "${PANEL_DIR}/.env" ]]; then
        secret=$(grep -E '^PANEL_SECRET_KEY=' "${PANEL_DIR}/.env" | cut -d= -f2- || true)
        steam_key=$(grep -E '^STEAM_WEB_API_KEY=' "${PANEL_DIR}/.env" | cut -d= -f2- || true)
    fi

    cat > "${PANEL_DIR}/.env" << EOF
PANEL_HOST=0.0.0.0
PANEL_PORT=${PANEL_PORT}
PANEL_SECRET_KEY=${secret}
DST_DIR=${DST_DIR}
DATABASE_PATH=/var/lib/dst-panel/data.db
DST_PANEL_DB=/var/lib/dst-panel/data.db
STEAM_WEB_API_KEY=${steam_key}
EOF
    chown "${DST_USER}:${DST_USER}" "${PANEL_DIR}/.env"
}

install_steamcmd_if_needed() {
    if [[ -f "${STEAMCMD_DIR}/steamcmd.sh" ]]; then
        log_info "SteamCMD already installed"
        return 0
    fi

    log_info "Installing SteamCMD..."
    su - "${DST_USER}" -c "
        cd '${STEAMCMD_DIR}'
        curl -sqL 'https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz' -o steamcmd.tar.gz
        tar -xzf steamcmd.tar.gz
        rm -f steamcmd.tar.gz
    "
}

steamcmd_link_steamclient() {
    local src32="" src64=""

    for candidate in \
        "/home/${DST_USER}/.steam/steamcmd/linux32/steamclient.so" \
        "${STEAMCMD_DIR}/linux32/steamclient.so"; do
        [[ -f "$candidate" ]] && src32="$candidate" && break
    done

    for candidate in \
        "/home/${DST_USER}/.steam/steamcmd/linux64/steamclient.so" \
        "${STEAMCMD_DIR}/linux64/steamclient.so"; do
        [[ -f "$candidate" ]] && src64="$candidate" && break
    done

    [[ -n "$src32" && -d "${DST_DIR}/bin/lib32" ]] && \
        cp -f "$src32" "${DST_DIR}/bin/lib32/steamclient.so" 2>/dev/null || true
    [[ -n "$src64" && -d "${DST_DIR}/bin/lib64" ]] && \
        cp -f "$src64" "${DST_DIR}/bin/lib64/steamclient.so" 2>/dev/null || true
}

steamcmd_install_dst() {
    local validate="${1:-0}"
    local max_attempts=4
    local validate_arg=""

    mkdir -p "${DST_DIR}/steamapps" "${STEAMCMD_DIR}"
    chown -R "${DST_USER}:${DST_USER}" "${DST_DIR}"

    if [[ ! -f "${STEAMCMD_DIR}/steamcmd.sh" ]]; then
        log_error "steamcmd.sh not found in ${STEAMCMD_DIR}"
        return 1
    fi

    [[ "$validate" == "1" ]] && validate_arg="validate"

    local attempt
    for attempt in $(seq 1 "$max_attempts"); do
        log_info "DST via SteamCMD (attempt ${attempt}/${max_attempts})..."

        set +e
        su - "${DST_USER}" -c "
            cd '${STEAMCMD_DIR}' && \
            ./steamcmd.sh \
                +@sSteamCmdForcePlatformType linux \
                +@NoPromptForPassword 1 \
                +@ShutdownOnFailedCommand 0 \
                +force_install_dir '${DST_DIR}' \
                +login anonymous \
                +app_update 343050 ${validate_arg} \
                +quit
        "
        set -e

        if [[ -f "$DST_BINARY" ]]; then
            steamcmd_link_steamclient
            log_info "DST server ready: ${DST_BINARY}"
            return 0
        fi

        [[ $attempt -lt $max_attempts ]] && sleep 5
    done

    return 1
}

preserve_cluster_token() {
    if [[ -s "${CLUSTER_DIR}/cluster_token.txt" ]]; then
        install -m 600 -o "${DST_USER}" -g "${DST_USER}" \
            "${CLUSTER_DIR}/cluster_token.txt" "${TOKEN_BACKUP}"
        log_info "Резервная копия Cluster Token сохранена"
    fi
}

restore_cluster_token_if_missing() {
    if [[ -s "${CLUSTER_DIR}/cluster_token.txt" ]]; then
        return 0
    fi
    if [[ ! -s "${TOKEN_BACKUP}" ]]; then
        return 0
    fi
    install -m 600 -o "${DST_USER}" -g "${DST_USER}" \
        "${TOKEN_BACKUP}" "${CLUSTER_DIR}/cluster_token.txt"
    log_warn "Cluster Token восстановлен из резервной копии"
}

install_dst_if_needed() {
    if [[ "${SKIP_DST:-0}" == "1" ]]; then
        log_info "Skipping DST (SKIP_DST=1)"
        return 0
    fi

    install_steamcmd_if_needed

    if [[ -f "$DST_BINARY" && "${UPDATE_DST:-0}" != "1" ]]; then
        log_info "DST already installed — skip (UPDATE_DST=1 to update)"
        return 0
    fi

    local validate=0
    [[ "${UPDATE_DST:-0}" == "1" ]] && validate=1

    if [[ "$validate" == "1" ]]; then
        log_info "Updating DST server files..."
        preserve_cluster_token
        systemctl stop dst-master.service dst-caves.service 2>/dev/null || true
    else
        log_info "Installing DST Dedicated Server..."
    fi

    if steamcmd_install_dst "$validate"; then
        restore_cluster_token_if_missing
        [[ "$validate" == "1" ]] && systemctl start dst-master.service dst-caves.service 2>/dev/null || true
        return 0
    fi

    if [[ -f "$DST_BINARY" ]]; then
        log_warn "SteamCMD reported errors, but DST binary exists — continuing"
        return 0
    fi

    log_error "DST installation failed. Retry: UPDATE_DST=1 bash $0"
    return 1
}

if [[ $EUID -ne 0 ]]; then
    log_error "This script must be run as root"
    exit 1
fi

if ! command -v systemctl &>/dev/null; then
    log_error "systemd is required but not found"
    exit 1
fi

OS=""
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS=$ID
fi

if [[ -d "${PANEL_DIR}/venv" ]]; then
    log_info "Existing installation detected — updating..."
else
    log_info "Fresh installation..."
fi

log_info "Detected OS: ${OS}"

if [[ "${SKIP_DEPS:-0}" != "1" ]]; then
    log_info "Installing system packages..."
    case $OS in
        ubuntu|debian) install_ubuntu_deps ;;
        centos|rhel|fedora)
            dnf install -y python3 python3-pip curl wget tar gzip glibc.i686 libstdc++.i686 \
                libcurl.i686 openssl-devel unzip git sqlite3
            ;;
        *)
            log_warn "Unknown OS: ${OS}, trying apt..."
            apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv \
                curl wget tar gzip unzip git sqlite3 2>/dev/null || true
            ;;
    esac
fi

if ! id "${DST_USER}" &>/dev/null; then
    log_info "Creating user '${DST_USER}'..."
    useradd -r -m -s /bin/bash -d "/home/${DST_USER}" "${DST_USER}"
else
    log_info "User '${DST_USER}' exists"
fi

log_info "Ensuring directories..."
migrate_cluster_layout
mkdir -p "${PANEL_DIR}" "${DST_DIR}" "${KLEI_DIR}" "${CLUSTER_DIR}" "${MASTER_DIR}" "${CAVES_DIR}" \
    "${BACKUP_DIR}" "${STEAMCMD_DIR}" "${PANEL_DIR}/scripts" "/var/lib/dst-panel/shard-logs" "/var/lib/dst-panel/world-library"

preserve_cluster_token

log_info "Updating panel files..."
cp -r "${PROJECT_DIR}/app" "${PANEL_DIR}/"
cp -f "${SCRIPT_DIR}/run_panel.sh" "${SCRIPT_DIR}/backup.sh" "${SCRIPT_DIR}/update.sh" "${PANEL_DIR}/scripts/"
cp -f "${SCRIPT_DIR}/install.sh" "${PANEL_DIR}/scripts/"
[[ -f "${PROJECT_DIR}/requirements.txt" ]] && cp "${PROJECT_DIR}/requirements.txt" "${PANEL_DIR}/"

log_info "Setting up Python environment..."
if [[ ! -d "${PANEL_DIR}/venv" ]]; then
    python3 -m venv "${PANEL_DIR}/venv"
fi
# shellcheck disable=SC1091
source "${PANEL_DIR}/venv/bin/activate"
pip install --upgrade pip --quiet
if [[ -f "${PANEL_DIR}/requirements.txt" ]]; then
    pip install -r "${PANEL_DIR}/requirements.txt" --quiet
else
    pip install fastapi "uvicorn[standard]" sqlmodel pyotp psutil aiofiles python-multipart --quiet
fi

log_info "Installing systemd services..."
cp "${PROJECT_DIR}/systemd/dst-panel.service" /etc/systemd/system/
cp "${PROJECT_DIR}/systemd/dst-master.service" /etc/systemd/system/
cp "${PROJECT_DIR}/systemd/dst-caves.service" /etc/systemd/system/
chmod +x "${PANEL_DIR}/scripts/run_panel.sh"

write_panel_env

chown -R "${DST_USER}:${DST_USER}" "/home/${DST_USER}"
chown -R "${DST_USER}:${DST_USER}" "${PANEL_DIR}"
chown -R "${DST_USER}:${DST_USER}" "/var/lib/dst-panel"

SHARD_REGISTRY="/var/lib/dst-panel/shard-pids.json"
if [[ ! -f "${SHARD_REGISTRY}" ]]; then
    echo '{"Master":null,"Caves":null}' > "${SHARD_REGISTRY}"
    chown "${DST_USER}:${DST_USER}" "${SHARD_REGISTRY}"
    chmod 600 "${SHARD_REGISTRY}"
fi

install_dst_if_needed

restore_cluster_token_if_missing
preserve_cluster_token

if [[ -d "${PANEL_DIR}" && -f "${PANEL_DIR}/app/config/config_reader.py" ]]; then
    log_info "Проверка привязки шардов (Master/Caves)..."
    sudo -u "${DST_USER}" env DST_DIR="${DST_DIR}" python3 -c "
import sys
sys.path.insert(0, '${PANEL_DIR}')
from app.config.config_reader import ensure_shard_link_config
r = ensure_shard_link_config()
if r.get('changed'):
    print('Исправлено:', ', '.join(r.get('items', [])))
" 2>/dev/null || log_warn "Не удалось проверить привязку шардов (запустится при старте панели)"
fi

touch "${CLUSTER_DIR}/adminlist.txt" "${CLUSTER_DIR}/blocklist.txt" "${CLUSTER_DIR}/whitelist.txt"

if [[ -s "${CLUSTER_DIR}/cluster_token.txt" ]]; then
    chmod 600 "${CLUSTER_DIR}/cluster_token.txt"
    chown "${DST_USER}:${DST_USER}" "${CLUSTER_DIR}/cluster_token.txt"
    log_info "Cluster Token на месте (не затрагивается переустановкой панели)"
else
    log_warn "Cluster Token не задан (нужен для онлайн-сервера в списке Klei)"
    log_warn "Добавьте в панели: раздел «Запуск» или «Конфиг → Токен»"
    log_warn "Получить токен: https://accounts.klei.com/account/game/servers"
    log_warn "Для LAN без токена включите «Офлайн-кластер» в cluster.ini"
fi

log_info "Restarting panel..."
systemctl daemon-reload
systemctl enable dst-panel.service
systemctl restart dst-panel.service

sleep 2
if curl -sf "http://127.0.0.1:${PANEL_PORT}/api/health" >/dev/null; then
    log_info "Panel health check passed"
else
    log_warn "Panel health check failed — check: journalctl -u dst-panel -n 50 --no-pager"
fi

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

log_info ""
log_info "======================================"
log_info "  DST Panel — done"
log_info "======================================"
log_info "  URL:      http://${SERVER_IP}:${PANEL_PORT}/"
log_info "  Login:    admin / admin123"
log_info "  Re-run:   bash scripts/install.sh"
log_info "  DST upd:  UPDATE_DST=1 bash scripts/install.sh"
log_info "  Firewall: ufw allow ${PANEL_PORT}/tcp"
log_info "======================================"
