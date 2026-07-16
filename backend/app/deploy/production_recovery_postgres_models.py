"""Pure contracts for the isolated PostgreSQL recovery rehearsal."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.deploy.backup_models import SHA256_PATTERN
from app.deploy.production_recovery_models import StableRecoveryLockIdentity

RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
DATABASE_PATTERN = re.compile(
    r"^tg_hub_4c_(?:source|replacement)_[0-9a-f]{16}$"
)
TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
STABLE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")

RehearsalPhase = Literal[
    "planned",
    "source_create_started",
    "source_created",
    "source_identity_commit_started",
    "source_identity_committed",
    "dump_started",
    "dump_committed",
    "replacement_bound",
    "workflow_terminal",
    "source_cleanup_started",
    "source_cleanup_completed",
    "rehearsal_terminal",
    "manual_reconciliation_required",
]
CleanupStatus = Literal["pending", "completed", "manual_reconciliation"]
RestoreDisposition = Literal[
    "restore_allowed", "already_complete", "partial", "manual_reconciliation", "absent"
]


class RehearsalContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TempPostgresRehearsalRecord(RehearsalContract):
    schema_version: Literal[1] = 1
    run_id: str
    server_identity_digest: str
    connection_identity_digest: str
    role_oid: int = Field(gt=0)
    role_identity_digest: str
    source_database_identity: str
    source_identity_token: str
    replacement_database_identity: str
    replacement_identity_token: str
    dump_identity: str | None = None
    workflow_record_id: str | None = None
    stable_lock_identity: StableRecoveryLockIdentity | None = None
    phase: RehearsalPhase = "planned"
    source_cleanup_status: CleanupStatus = "pending"
    replacement_cleanup_status: CleanupStatus = "pending"
    residue_count: int | None = Field(default=None, ge=0)
    last_error_code: str | None = None

    @field_validator("run_id", "workflow_record_id")
    @classmethod
    def validate_opaque_id(cls, value: str | None) -> str | None:
        if value is not None and not RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid opaque id")
        return value

    @field_validator(
        "server_identity_digest",
        "connection_identity_digest",
        "role_identity_digest",
        "dump_identity",
    )
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_PATTERN.fullmatch(value):
            raise ValueError("invalid digest")
        return value

    @field_validator("source_database_identity", "replacement_database_identity")
    @classmethod
    def validate_database(cls, value: str) -> str:
        if not DATABASE_PATTERN.fullmatch(value):
            raise ValueError("invalid generated database identity")
        return value

    @field_validator("source_identity_token", "replacement_identity_token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        if not TOKEN_PATTERN.fullmatch(value):
            raise ValueError("invalid identity token")
        return value

    @field_validator("last_error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and not STABLE_CODE_PATTERN.fullmatch(value):
            raise ValueError("invalid stable error code")
        return value

    @model_validator(mode="after")
    def validate_terminal(self) -> "TempPostgresRehearsalRecord":
        if self.source_database_identity == self.replacement_database_identity:
            raise ValueError("source and replacement must differ")
        if self.phase == "rehearsal_terminal" and (
            self.source_cleanup_status != "completed"
            or self.replacement_cleanup_status != "completed"
            or self.residue_count != 0
        ):
            raise ValueError("rehearsal terminal requires complete cleanup")
        if self.phase == "manual_reconciliation_required" and not (
            self.source_cleanup_status == "manual_reconciliation"
            or self.replacement_cleanup_status == "manual_reconciliation"
        ):
            raise ValueError("manual phase requires manual cleanup status")
        if self.phase in {
            "source_cleanup_started",
            "source_cleanup_completed",
            "rehearsal_terminal",
        } and self.replacement_cleanup_status != "completed":
            raise ValueError("source cleanup requires replacement cleanup")
        if self.phase in {
            "source_cleanup_completed",
            "rehearsal_terminal",
        } and self.source_cleanup_status != "completed":
            raise ValueError("source cleanup completion requires observed cleanup")
        return self


class GeneratedDatabaseFacts(RehearsalContract):
    exists: bool
    identity_matches: bool = False
    catalog_state: Literal["empty", "complete", "partial", "unknown"] = "unknown"


class CatalogObject(RehearsalContract):
    object_type: Literal[
        "schema", "relation", "sequence", "function", "type", "extension", "other"
    ]
    schema_name: str
    name: str
    user_owned: bool
    template_allowed: bool = False


class ToolchainObservation(RehearsalContract):
    server_major: int = Field(gt=0)
    pg_dump_major: int = Field(gt=0)
    pg_restore_major: int = Field(gt=0)


class LiveGeneratedDatabaseState(RehearsalContract):
    exists: bool
    owner_oid: int | None = Field(default=None, gt=0)
    comment: str | None = None
    active_connections: int = Field(ge=0)
    prepared_transactions: int = Field(ge=0)
    catalog_state: Literal["not_checked", "empty", "partial", "complete", "unknown"]


class RehearsalCommand(RehearsalContract):
    argv: tuple[str, ...]
    env: dict[str, str]


class TempPostgresRehearsalResult(RehearsalContract):
    status: Literal["success", "fail"]
    run_id: str
    phase: RehearsalPhase
    source_created: bool
    dump_validated: bool
    replacement_created: bool
    restore_completed: bool
    verification_completed: bool
    config_switched: bool
    rollback_completed: bool
    replacement_cleanup_completed: bool
    source_cleanup_completed: bool
    residue_count: int | None
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not RUN_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid run id")
        return value

    @model_validator(mode="after")
    def validate_success(self) -> "TempPostgresRehearsalResult":
        if self.status == "success" and not (
            self.rollback_completed
            and self.replacement_cleanup_completed
            and self.source_cleanup_completed
            and self.residue_count == 0
            and self.error_code is None
        ):
            raise ValueError("success requires rehearsal terminal")
        return self


FORWARD_PHASES: dict[RehearsalPhase, frozenset[RehearsalPhase]] = {
    "planned": frozenset({"source_create_started"}),
    "source_create_started": frozenset(
        {"source_created", "manual_reconciliation_required"}
    ),
    "source_created": frozenset(
        {"source_identity_commit_started", "manual_reconciliation_required"}
    ),
    "source_identity_commit_started": frozenset(
        {"source_identity_committed", "manual_reconciliation_required"}
    ),
    "source_identity_committed": frozenset(
        {"dump_started", "manual_reconciliation_required"}
    ),
    "dump_started": frozenset({"dump_committed", "manual_reconciliation_required"}),
    "dump_committed": frozenset(
        {"replacement_bound", "manual_reconciliation_required"}
    ),
    "replacement_bound": frozenset(
        {"workflow_terminal", "manual_reconciliation_required"}
    ),
    "workflow_terminal": frozenset(
        {"source_cleanup_started", "manual_reconciliation_required"}
    ),
    "source_cleanup_started": frozenset(
        {"source_cleanup_completed", "manual_reconciliation_required"}
    ),
    "source_cleanup_completed": frozenset(
        {"rehearsal_terminal", "manual_reconciliation_required"}
    ),
    "rehearsal_terminal": frozenset(),
    "manual_reconciliation_required": frozenset(),
}


def advance_rehearsal(
    record: TempPostgresRehearsalRecord, target: RehearsalPhase
) -> TempPostgresRehearsalRecord:
    if target not in FORWARD_PHASES[record.phase]:
        raise ValueError("TEMP_REHEARSAL_PHASE_INVALID")
    try:
        return TempPostgresRehearsalRecord.model_validate(
            {**record.model_dump(mode="python"), "phase": target}
        )
    except ValueError as exc:
        raise ValueError("TEMP_REHEARSAL_PHASE_INVALID") from exc


def classify_restore_facts(facts: GeneratedDatabaseFacts) -> RestoreDisposition:
    if not facts.exists:
        return "absent"
    if not facts.identity_matches or facts.catalog_state == "unknown":
        return "manual_reconciliation"
    return {
        "empty": "restore_allowed",
        "complete": "already_complete",
        "partial": "partial",
        "unknown": "manual_reconciliation",
    }[facts.catalog_state]


def classify_empty_catalog(
    objects: tuple[CatalogObject, ...],
) -> Literal["empty", "partial"]:
    system_schemas = {"pg_catalog", "information_schema"}
    for item in objects:
        if item.schema_name in system_schemas and not item.user_owned:
            continue
        if (
            item.object_type == "schema"
            and item.schema_name == "public"
            and item.name == "public"
        ):
            continue
        if item.template_allowed:
            continue
        if item.user_owned or item.schema_name not in system_schemas:
            return "partial"
    return "empty"


def validate_toolchain(observation: ToolchainObservation) -> None:
    if observation.pg_dump_major != observation.pg_restore_major:
        raise ValueError("TEMP_REHEARSAL_TOOL_VERSION_UNSUPPORTED")
    if observation.pg_dump_major < observation.server_major:
        raise ValueError("TEMP_REHEARSAL_TOOL_VERSION_UNSUPPORTED")
