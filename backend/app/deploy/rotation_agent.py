"""Safe lifecycle metadata for the local rotation LaunchAgent."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings, load_settings
from app.deploy.rotation_fs import (
    RotationFsError,
    atomic_write_json,
    fsync_directory,
    validate_directory,
)

ROTATION_AGENT_LABEL = "com.tghub.rotate-logs"
METADATA_FILENAME = "rotation-agent.json"


def metadata_path(settings: Settings) -> Path:
    return settings.HEARTBEAT_PATH.expanduser().parent / METADATA_FILENAME


def record_install(
    settings: Settings,
    *,
    installed_at: datetime | None = None,
) -> Path:
    path = metadata_path(settings)
    validate_directory(path.parent, create=True)
    timestamp = installed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("installed_at must be timezone-aware")
    payload = {
        "schema_version": 1,
        "label": ROTATION_AGENT_LABEL,
        "installed_at": timestamp.astimezone(timezone.utc).isoformat(),
    }
    atomic_write_json(path, payload, token=secrets.token_hex(8))
    return path


def remove_install(settings: Settings) -> None:
    path = metadata_path(settings)
    try:
        path.parent.lstat()
    except FileNotFoundError:
        return
    validate_directory(path.parent, create=False)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise RotationFsError("ROTATION_PATH_INVALID")
    try:
        path.unlink()
        fsync_directory(path.parent)
    except OSError as exc:
        raise RotationFsError("ROTATION_AGENT_METADATA_REMOVE_FAILED") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tg-hub-rotation-agent")
    parser.add_argument("action", choices=("record-install", "remove-install"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(os.environ.get("TG_HUB_ENV_FILE"))
    try:
        if args.action == "record-install":
            record_install(settings)
        else:
            remove_install(settings)
    except Exception:
        error_code = (
            "ROTATION_AGENT_METADATA_WRITE_FAILED"
            if args.action == "record-install"
            else "ROTATION_AGENT_METADATA_REMOVE_FAILED"
        )
        print(
            json.dumps(
                {"status": "fail", "error_code": error_code},
                separators=(",", ":"),
            )
        )
        return 1
    print(
        json.dumps(
            {"status": "pass", "action": args.action},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
