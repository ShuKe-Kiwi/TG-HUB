"""Launchd wrapper: validate production startup, then replace itself with Uvicorn."""

import asyncio
import json
import os
from pathlib import Path

from app.config import load_settings
from app.deploy.preflight import run_startup_preflight


def _write_state(status: str, error_code: str | None = None) -> None:
    path = Path.home() / ".tg-hub/runtime/deploy-state.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"status": status, "error_code": error_code}), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    selected = os.environ.get("TG_HUB_ENV_FILE")
    if not selected:
        _write_state("blocked", "ENV_FILE_NOT_CONFIGURED")
        return 0
    report = asyncio.run(run_startup_preflight(Path(selected)))
    print(report.model_dump_json(), flush=True)
    if report.status == "fail":
        _write_state("blocked", report.error_code or "STATIC_PREFLIGHT_FAILED")
        return report.exit_code if report.exit_code == 13 else 0
    load_settings(selected)
    _write_state("starting")
    os.execv(
        os.sys.executable,
        [os.sys.executable, "-m", "app.deploy.server"],
    )
    return 16


if __name__ == "__main__":
    raise SystemExit(main())
