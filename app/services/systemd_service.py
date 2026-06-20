import os
import subprocess

SYSTEMD_DIR = "/etc/systemd/system"


def ensure_service_file(name: str, content: str) -> dict:
    try:
        path = f"{SYSTEMD_DIR}/{name}"
        with open(path, "w") as f:
            f.write(content)
        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_service_status(name: str) -> dict:
    try:
        import subprocess
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True, text=True
        )
        active = result.stdout.strip() == "active"
        result = subprocess.run(
            ["systemctl", "is-enabled", name],
            capture_output=True, text=True
        )
        enabled = result.stdout.strip() == "enabled"
        return {"active": active, "enabled": enabled}
    except Exception as e:
        return {"active": False, "enabled": False, "error": str(e)}
