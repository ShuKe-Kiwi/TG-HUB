"""Stable, desensitized contracts for local log rotation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TargetKey = Literal["app.stdout.log", "app.stderr.log", "heartbeat.jsonl"]
TriggerReason = Literal["size", "age", "none", "recovery"]
TargetResult = Literal["rotated", "not_modified", "recovered", "failed"]
RunStatus = Literal["pass", "partial", "fail"]
BudgetStatus = Literal["within_budget", "cleaned", "exceeded_unrecoverable"]
PendingPhase = Literal["snapshot_committed", "active_truncated"]

ROTATION_ERROR_CODES = frozenset(
    {
        "ROTATION_ALREADY_RUNNING",
        "ROTATION_PATH_INVALID",
        "ROTATION_PERMISSION_DENIED",
        "ROTATION_ACTIVE_NOT_REGULAR",
        "ROTATION_READ_FAILED",
        "ROTATION_WRITE_FAILED",
        "ROTATION_FSYNC_FAILED",
        "ROTATION_TRUNCATE_FAILED",
        "ROTATION_COMPRESS_FAILED",
        "ROTATION_STATUS_WRITE_FAILED",
        "ROTATION_RECOVERY_REQUIRED",
        "ROTATION_HEARTBEAT_RECOVERY_REQUIRED",
        "ROTATION_BUDGET_EXCEEDED_UNRECOVERABLE",
        "ROTATION_UNEXPECTED_ERROR",
    }
)


class RotationFileStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    last_checked_at: datetime | None = None
    active_generation_started_at: datetime | None = None
    last_rotated_at: datetime | None = None
    last_size_bytes: int = 0
    trigger_reason: TriggerReason = "none"
    result: TargetResult = "not_modified"
    error_code: str | None = None
    archive_name: str | None = None
    copied_bytes: int = 0
    complete_line_bytes: int = 0
    truncated_bytes: int = 0
    partial_tail_detected: bool = False
    discarded_partial_tail_bytes: int = 0
    legacy_content_possible: bool = False


class RotationStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    run_id: str
    check_started_at: datetime
    check_completed_at: datetime
    status: RunStatus
    error_code: str | None = None
    rotated_files: int = 0
    cleaned_archives: int = 0
    archive_bytes: int = 0
    active_bytes: int = 0
    total_observed_bytes: int = 0
    archive_budget_status: BudgetStatus = "within_budget"
    active_oversize: bool = False
    files: dict[TargetKey, RotationFileStatus]
    rotation_consistency: Literal["best_effort_copy_truncate"] = (
        "best_effort_copy_truncate"
    )
    concurrent_write_loss_possible: Literal[True] = True
    writer_paused: Literal[False] = False
    report_desensitized: Literal["yes"] = "yes"


class PendingMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    target: Literal["app.stdout.log", "app.stderr.log"]
    run_id: str
    phase: PendingPhase
    active_inode: int
    snapshot_size: int = Field(ge=0)
    complete_line_bytes: int = Field(ge=0)
    created_at: datetime


class RotationRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: RunStatus
    error_code: str | None = None
    rotated_files: int = 0
    cleaned_archives: int = 0
    archive_budget_status: BudgetStatus = "within_budget"
    active_oversize: bool = False
    report_desensitized: Literal["yes"] = "yes"
