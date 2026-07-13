"""Read-only, desensitized projection of local rotation status."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.config import Settings, load_settings
from app.deploy.rotation_agent import METADATA_FILENAME, ROTATION_AGENT_LABEL
from app.deploy.rotation_models import ROTATION_ERROR_CODES, RotationStatus

MAX_STATUS_BYTES = 1024 * 1024
STALE_AFTER = timedelta(hours=2, minutes=15)
MAX_FUTURE_SKEW = timedelta(minutes=5)

ProjectionStatus = Literal["never_run", "pass", "partial", "fail", "invalid"]
AgentStatus = Literal["not_configured", "configured", "invalid"]
ProjectionStale = bool | Literal["not_applicable", "unknown"]


class RotationAgentMetadata(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    label: Literal["com.tghub.rotate-logs"] = ROTATION_AGENT_LABEL
    installed_at: datetime


class RotationStatusProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_status: AgentStatus
    status: ProjectionStatus
    error_code: str | None = None
    installed_at: datetime | None = None
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    rotated_files: int | None = None
    cleaned_archives: int | None = None
    archive_bytes: int | None = None
    active_bytes: int | None = None
    archive_budget_status: Literal[
        "within_budget", "cleaned", "exceeded_unrecoverable", "unknown"
    ] = "unknown"
    active_oversize: bool | None = None
    legacy_content_possible: bool | None = None
    stale: ProjectionStale
    report_desensitized: Literal["yes"] = "yes"


class _ReadError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


def _nofollow() -> int:
    return getattr(os, "O_NOFOLLOW", 0)


def _read_json_object(path: Path, *, error_prefix: str) -> dict | None:
    try:
        parent_info = path.parent.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _ReadError(f"{error_prefix}_READ_FAILED") from exc
    if (
        stat.S_ISLNK(parent_info.st_mode)
        or not stat.S_ISDIR(parent_info.st_mode)
        or stat.S_IMODE(parent_info.st_mode) & 0o077
    ):
        raise _ReadError(f"{error_prefix}_INVALID")

    try:
        fd = os.open(path, os.O_RDONLY | _nofollow())
    except FileNotFoundError:
        return None
    except PermissionError as exc:
        raise _ReadError(f"{error_prefix}_READ_FAILED") from exc
    except OSError as exc:
        raise _ReadError(f"{error_prefix}_INVALID") from exc

    try:
        first = os.fstat(fd)
        if not stat.S_ISREG(first.st_mode) or stat.S_IMODE(first.st_mode) & 0o077:
            raise _ReadError(f"{error_prefix}_INVALID")
        size = first.st_size
        if size < 0 or size > MAX_STATUS_BYTES:
            raise _ReadError(f"{error_prefix}_INVALID")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                raise _ReadError(f"{error_prefix}_READ_FAILED")
            chunks.append(chunk)
            remaining -= len(chunk)
        second = os.fstat(fd)
        if first.st_ino != second.st_ino or second.st_size != size:
            raise _ReadError(f"{error_prefix}_READ_FAILED")
    except _ReadError:
        raise
    except OSError as exc:
        raise _ReadError(f"{error_prefix}_READ_FAILED") from exc
    finally:
        os.close(fd)

    try:
        payload = json.loads(b"".join(chunks).decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ReadError(f"{error_prefix}_INVALID") from exc
    if not isinstance(payload, dict):
        raise _ReadError(f"{error_prefix}_INVALID")
    return payload


def _aware_utc(value: datetime, *, now: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _ReadError("ROTATION_STATUS_TIME_INVALID")
    normalized = value.astimezone(timezone.utc)
    if normalized > now + MAX_FUTURE_SKEW:
        raise _ReadError("ROTATION_STATUS_TIME_INVALID")
    return normalized


def _empty_values() -> dict:
    return {
        "rotated_files": 0,
        "cleaned_archives": 0,
        "archive_bytes": 0,
        "active_bytes": 0,
        "archive_budget_status": "within_budget",
        "active_oversize": False,
        "legacy_content_possible": False,
    }


def _unknown_values() -> dict:
    return {
        "rotated_files": None,
        "cleaned_archives": None,
        "archive_bytes": None,
        "active_bytes": None,
        "archive_budget_status": "unknown",
        "active_oversize": None,
        "legacy_content_possible": None,
    }


def _parse_agent(
    payload: dict | None, *, now: datetime
) -> tuple[AgentStatus, datetime | None, str | None]:
    if payload is None:
        return "not_configured", None, None
    try:
        metadata = RotationAgentMetadata.model_validate(payload)
        installed_at = _aware_utc(metadata.installed_at, now=now)
    except (ValidationError, _ReadError):
        return "invalid", None, "ROTATION_AGENT_METADATA_INVALID"
    return "configured", installed_at, None


def _parse_status(
    payload: dict | None, *, now: datetime
) -> tuple[RotationStatus | None, str | None]:
    if payload is None:
        return None, None
    if payload.get("schema_version") != 1:
        return None, "ROTATION_STATUS_SCHEMA_UNSUPPORTED"
    try:
        status = RotationStatus.model_validate(payload)
    except ValidationError:
        return None, "ROTATION_STATUS_INVALID"
    try:
        started = _aware_utc(status.check_started_at, now=now)
        completed = _aware_utc(status.check_completed_at, now=now)
    except _ReadError:
        return None, "ROTATION_STATUS_TIME_INVALID"
    if started > completed:
        return None, "ROTATION_STATUS_TIME_INVALID"
    return status.model_copy(
        update={"check_started_at": started, "check_completed_at": completed}
    ), None


def invalid_rotation_projection(
    error_code: str = "ROTATION_STATUS_READ_FAILED",
) -> RotationStatusProjection:
    return RotationStatusProjection(
        agent_status="invalid",
        status="invalid",
        error_code=error_code,
        stale="unknown",
        **_unknown_values(),
    )


def read_rotation_status(
    settings: Settings,
    *,
    now: datetime | None = None,
) -> RotationStatusProjection:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    runtime_dir = settings.HEARTBEAT_PATH.expanduser().parent

    try:
        agent_payload = _read_json_object(
            runtime_dir / METADATA_FILENAME,
            error_prefix="ROTATION_AGENT_METADATA",
        )
    except _ReadError as exc:
        return invalid_rotation_projection(exc.error_code)

    agent_status, installed_at, agent_error = _parse_agent(
        agent_payload, now=current
    )
    if agent_error:
        return invalid_rotation_projection(agent_error)

    try:
        status_payload = _read_json_object(
            runtime_dir / "rotation-status.json",
            error_prefix="ROTATION_STATUS",
        )
    except _ReadError as exc:
        return RotationStatusProjection(
            agent_status=agent_status,
            status="invalid",
            error_code=exc.error_code,
            installed_at=installed_at,
            stale="unknown",
            **_unknown_values(),
        )

    status, status_error = _parse_status(status_payload, now=current)
    if status_error:
        return RotationStatusProjection(
            agent_status=agent_status,
            status="invalid",
            error_code=status_error,
            installed_at=installed_at,
            stale="unknown",
            **_unknown_values(),
        )

    if status is None or (
        agent_status == "configured"
        and installed_at is not None
        and status.check_completed_at < installed_at
    ):
        stale: ProjectionStale
        if agent_status == "not_configured":
            stale = "not_applicable"
        elif agent_status == "configured" and installed_at is not None:
            stale = current - installed_at > STALE_AFTER
        else:
            stale = "unknown"
        return RotationStatusProjection(
            agent_status=agent_status,
            status="never_run",
            installed_at=installed_at,
            stale=stale,
            **_empty_values(),
        )

    stale = (
        "not_applicable"
        if agent_status == "not_configured"
        else current - status.check_completed_at > STALE_AFTER
    )
    execution_error = status.error_code
    if execution_error is not None and execution_error not in ROTATION_ERROR_CODES:
        execution_error = "ROTATION_STATUS_ERROR_UNKNOWN"
    return RotationStatusProjection(
        agent_status=agent_status,
        status=status.status,
        error_code=execution_error,
        installed_at=installed_at,
        last_started_at=status.check_started_at,
        last_completed_at=status.check_completed_at,
        rotated_files=status.rotated_files,
        cleaned_archives=status.cleaned_archives,
        archive_bytes=status.archive_bytes,
        active_bytes=status.active_bytes,
        archive_budget_status=status.archive_budget_status,
        active_oversize=status.active_oversize,
        legacy_content_possible=any(
            item.legacy_content_possible for item in status.files.values()
        ),
        stale=stale,
    )


def main() -> int:
    projection = read_rotation_status(load_settings())
    print(projection.model_dump_json())
    if projection.agent_status == "invalid" or projection.status == "invalid":
        return 2
    if projection.agent_status == "not_configured":
        return 0
    if projection.status in {"partial", "fail"}:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
