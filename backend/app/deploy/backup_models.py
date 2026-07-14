"""Stable contracts for P6-Deploy-4 backup and restore verification."""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BACKUP_ID_PATTERN = re.compile(
    r"^[0-9]{8}T[0-9]{6}\.[0-9]{6}Z-[0-9a-f]{32}$"
)
RESTORE_TARGET_PATTERN = re.compile(
    r"^tg_hub_restore_verify_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{16}$"
)
OPAQUE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
IDENTITY_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")

PackageState = Literal[
    "not_created", "temp_only", "final_committed", "commit_uncertain"
]
RecoveryPhase = Literal[
    "planned",
    "identity_committed",
    "restore_started",
    "restore_failed",
    "verification_failed",
]

BACKUP_ERROR_CODES = frozenset(
    {
        "BACKUP_IN_PROGRESS",
        "BACKUP_PATH_INVALID",
        "BACKUP_SPACE_INSUFFICIENT",
        "BACKUP_TOOL_MISSING",
        "PG_DUMP_VERSION_UNSUPPORTED",
        "DATABASE_UNAVAILABLE",
        "MIGRATION_NOT_AT_HEAD",
        "GIT_WORKTREE_DIRTY",
        "WATCHLIST_UNREADABLE",
        "WATCHLIST_CHANGED_DURING_BACKUP",
        "DATABASE_DUMP_FAILED",
        "DATABASE_DUMP_INVALID",
        "BACKUP_MANIFEST_INVALID",
        "BACKUP_CHECKSUM_MISMATCH",
        "BACKUP_COMMIT_UNCERTAIN",
        "BACKUP_CLEANUP_REQUIRED",
        "BACKUP_SUBPROCESS_CLEANUP_FAILED",
        "RESTORE_TARGET_UNSAFE",
        "RESTORE_TARGET_IDENTITY_UNCOMMITTED",
        "RESTORE_RECOVERY_RECORD_WRITE_FAILED",
        "RESTORE_RECOVERY_RECORD_INVALID",
        "RESTORE_TOOL_MISSING",
        "PG_RESTORE_VERSION_UNSUPPORTED",
        "RESTORE_CREATE_FAILED",
        "RESTORE_FAILED",
        "RESTORE_CODE_REVISION_MISMATCH",
        "RESTORE_SCHEMA_INVALID",
        "RESTORE_INTEGRITY_FAILED",
        "RESTORE_DROP_FAILED",
        "RESTORE_SUBPROCESS_CLEANUP_FAILED",
    }
)


class ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DatabaseBackupManifest(ContractModel):
    format: Literal["postgresql_custom"] = "postgresql_custom"
    filename: Literal["database.dump"] = "database.dump"
    sha256: str
    size_bytes: int = Field(ge=0)
    source_server_version: str = Field(min_length=1, max_length=128)
    source_server_major: int = Field(ge=1)
    pg_dump_version: str = Field(min_length=1, max_length=128)
    pg_dump_major: int = Field(ge=1)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("invalid sha256")
        return value


class WatchlistBackupManifest(ContractModel):
    filename: Literal["watchlist.json"] = "watchlist.json"
    sha256: str
    size_bytes: int = Field(ge=0)
    revision: str = Field(min_length=1, max_length=128)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("invalid sha256")
        return value


class BackupExclusions(ContractModel):
    production_env: Literal["secret_material_excluded"]
    telethon_session: Literal["authentication_session_excluded"]
    logs: Literal["operational_data_excluded"]
    runtime_state: Literal["ephemeral_data_excluded"]


class BackupManifest(ContractModel):
    schema_version: Literal[1] = 1
    backup_id: str
    backup_status: Literal["complete"] = "complete"
    created_at_utc: datetime
    app_git_commit: str
    git_worktree_clean: Literal[True]
    python_version: str = Field(min_length=1, max_length=64)
    dependency_lock_filename: Literal["uv.lock"] = "uv.lock"
    dependency_lock_sha256: str
    alembic_revision: str = Field(min_length=1, max_length=128)
    config_schema_version: int = Field(ge=1)
    database: DatabaseBackupManifest
    watchlist: WatchlistBackupManifest
    exclusions: BackupExclusions

    @field_validator("backup_id")
    @classmethod
    def validate_backup_id(cls, value: str) -> str:
        return validate_backup_id(value)

    @field_validator("created_at_utc")
    @classmethod
    def validate_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at_utc must be timezone-aware")
        if value.utcoffset().total_seconds() != 0:
            raise ValueError("created_at_utc must use UTC")
        return value

    @field_validator("app_git_commit")
    @classmethod
    def validate_git_commit(cls, value: str) -> str:
        if not GIT_COMMIT_PATTERN.fullmatch(value):
            raise ValueError("invalid git commit")
        return value

    @field_validator("dependency_lock_sha256")
    @classmethod
    def validate_lock_sha256(cls, value: str) -> str:
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("invalid dependency lock sha256")
        return value

    @model_validator(mode="after")
    def validate_source_dump_major(self) -> BackupManifest:
        require_backup_version_compatibility(
            source_server_major=self.database.source_server_major,
            pg_dump_major=self.database.pg_dump_major,
        )
        return self


class BackupRunResult(ContractModel):
    status: Literal["pass", "fail"]
    backup_id: str | None = None
    package_state: PackageState
    manifest_valid: Literal["yes", "no"]
    database_dump_valid: Literal["yes", "no"]
    watchlist_snapshot_valid: Literal["yes", "no"]
    backup_bytes: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"

    @field_validator("backup_id")
    @classmethod
    def validate_optional_backup_id(cls, value: str | None) -> str | None:
        return validate_backup_id(value) if value is not None else None

    @model_validator(mode="after")
    def validate_state_mapping(self) -> BackupRunResult:
        final_exists = self.package_state in {"final_committed", "commit_uncertain"}
        if final_exists != (self.backup_id is not None):
            raise ValueError("backup_id must identify an existing final package")
        if self.status == "pass":
            if self.package_state != "final_committed" or self.error_code is not None:
                raise ValueError("pass requires a committed package without an error")
        elif self.package_state == "final_committed":
            raise ValueError("final_committed is reserved for successful backup")
        if self.package_state == "commit_uncertain" and (
            self.error_code != "BACKUP_COMMIT_UNCERTAIN"
        ):
            raise ValueError("commit_uncertain requires BACKUP_COMMIT_UNCERTAIN")
        if self.error_code is not None and self.error_code not in BACKUP_ERROR_CODES:
            raise ValueError("unknown backup error code")
        return self


class RestoreRecoveryRecord(ContractModel):
    schema_version: Literal[1] = 1
    opaque_id: str
    generated_target_name: str
    identity_token: str
    created_at: datetime
    backup_id: str
    phase: RecoveryPhase

    @field_validator("opaque_id")
    @classmethod
    def validate_opaque_id(cls, value: str) -> str:
        if not OPAQUE_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid opaque id")
        return value

    @field_validator("generated_target_name")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return validate_restore_target_name(value)

    @field_validator("identity_token")
    @classmethod
    def validate_identity_token(cls, value: str) -> str:
        if not IDENTITY_TOKEN_PATTERN.fullmatch(value):
            raise ValueError("invalid identity token")
        return value

    @field_validator("backup_id")
    @classmethod
    def validate_record_backup_id(cls, value: str) -> str:
        return validate_backup_id(value)

    @field_validator("created_at")
    @classmethod
    def validate_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value


def validate_backup_id(value: str) -> str:
    if not BACKUP_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid backup id")
    return value


def validate_restore_target_name(value: str) -> str:
    if not RESTORE_TARGET_PATTERN.fullmatch(value):
        raise ValueError("unsafe restore target name")
    return value


def generate_restore_identity(created_at: datetime) -> tuple[str, str, str]:
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    timestamp = created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        f"tg_hub_restore_verify_{timestamp}_{secrets.token_hex(8)}",
        secrets.token_hex(16),
        secrets.token_hex(32),
    )


def require_backup_version_compatibility(
    *, source_server_major: int, pg_dump_major: int
) -> None:
    if source_server_major < 1 or pg_dump_major < 1:
        raise ValueError("PostgreSQL major versions must be positive")
    if source_server_major != pg_dump_major:
        raise ValueError("PG_DUMP_VERSION_UNSUPPORTED")


def require_restore_version_compatibility(
    *, source_server_major: int, pg_dump_major: int, pg_restore_major: int,
    restore_target_server_major: int,
) -> None:
    versions = {
        source_server_major,
        pg_dump_major,
        pg_restore_major,
        restore_target_server_major,
    }
    if min(versions) < 1 or len(versions) != 1:
        raise ValueError("PG_RESTORE_VERSION_UNSUPPORTED")


def advance_recovery_phase(
    record: RestoreRecoveryRecord, phase: RecoveryPhase
) -> RestoreRecoveryRecord:
    allowed: dict[RecoveryPhase, set[RecoveryPhase]] = {
        "planned": {"identity_committed"},
        "identity_committed": {"restore_started"},
        "restore_started": {"restore_failed", "verification_failed"},
        "restore_failed": set(),
        "verification_failed": set(),
    }
    if phase not in allowed[record.phase]:
        raise ValueError("invalid recovery phase transition")
    return record.model_copy(update={"phase": phase})
