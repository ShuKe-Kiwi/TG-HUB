"""Combine launchd ownership and HTTP health without PID files."""

import json
import os
import subprocess
import urllib.error
import urllib.request

from app.config import load_settings


def _health(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return "pass" if response.status == 200 else "fail"
    except (OSError, urllib.error.URLError):
        return "fail"


def main() -> int:
    domain = f"gui/{os.getuid()}/com.tghub.service"
    launched = subprocess.run(
        ["launchctl", "print", domain],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    selected = os.environ.get("TG_HUB_ENV_FILE", "~/.tg-hub/production.env")
    try:
        settings = load_settings(selected)
        base = f"http://{settings.ADMIN_BIND_HOST}:{settings.ADMIN_PORT}"
        live = _health(f"{base}/health/live")
        ready = _health(f"{base}/health/ready")
    except Exception:
        live = ready = "unknown"
    print(json.dumps({"service": "running" if launched else "stopped", "liveness": live, "readiness": ready}))
    return 0 if launched else 1


if __name__ == "__main__":
    raise SystemExit(main())
