"""Launchd wrapper: validate production startup, then replace itself with Uvicorn."""

import asyncio
import json
import os
from pathlib import Path

from app.config import load_settings
from app.deploy.preflight import run_startup_preflight


def _write_state(error_code: str) -> None:
    path = Path.home() / ".tg-hub/runtime/deploy-state.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"status": "blocked", "error_code": error_code}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    selected = os.environ.get("TG_HUB_ENV_FILE")
    if not selected:
        _write_state("ENV_FILE_NOT_CONFIGURED")
        return 0
    report = asyncio.run(run_startup_preflight(Path(selected)))
    print(report.model_dump_json(), flush=True)
    if report.status == "fail":
        _write_state(report.error_code or "STATIC_PREFLIGHT_FAILED")
        return report.exit_code if report.exit_code == 13 else 0
    settings = load_settings(selected)
    os.execv(
        os.sys.executable,
        [os.sys.executable, "-m", "uvicorn", "app.main:app", "--host", settings.ADMIN_BIND_HOST, "--port", str(settings.ADMIN_PORT), "--workers", "1", "--no-access-log"],
    )
    return 16


if __name__ == "__main__":
    raise SystemExit(main())
