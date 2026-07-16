"""Strict contracts and pure transitions for production-recovery rehearsals."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.deploy.backup_models import BACKUP_ID_PATTERN, SHA256_PATTERN

YesNo = Literal["yes", "no"]
TriState = Literal["yes", "no", "unknown"]
RecoveryPhase = Literal[
    "planned", "protection_backup_started", "protection_backup_completed",
    "protection_backup_skipped_authorized", "replacement_create_started",
    "replacement_created", "identity_commit_started", "identity_committed",
    "restore_started", "restore_completed", "verification_started",
    "verification_completed", "services_stop_started", "services_stopped",
    "config_protection_started", "config_protection_completed",
    "env_switch_started", "env_switched", "watchlist_switch_started",
    "watchlist_switched", "config_switched", "application_start_started",
    "application_started", "readiness_started", "readiness_passed",
    "monitor_start_authorized", "monitor_start_started", "monitor_started",
    "monitor_write_observed", "completed", "rollback_started",
    "rollback_monitor_stopped", "rollback_application_stop_started",
    "rollback_application_stopped", "rollback_env_started",
    "rollback_env_completed", "rollback_watchlist_started",
    "rollback_watchlist_completed", "rollback_application_start_started",
    "rollback_application_started", "rollback_readiness_started",
    "rollback_readiness_passed", "rolled_back",
]

INCIDENT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
STABLE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")


class RecoveryContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceIdentities(RecoveryContract):
    original_database_revision: str
    replacement_database_identity: str
    expected_owner_identity: str
    original_env_sha256: str
    staged_env_sha256: str
    protected_env_sha256: str | None = None
    original_watchlist_sha256: str
    staged_watchlist_sha256: str | None = None
    protected_watchlist_sha256: str | None = None

    @field_validator(
        "original_env_sha256",
        "staged_env_sha256",
        "protected_env_sha256",
        "original_watchlist_sha256",
        "staged_watchlist_sha256",
        "protected_watchlist_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is not None and not SHA256_PATTERN.fullmatch(value):
            raise ValueError("invalid sha256 identity")
        return value


class AuthorizationObservations(RecoveryContract):
    protection_skip_authorized: YesNo = "no"
    watchlist_switch_authorized: YesNo = "no"
    monitor_start_authorized: YesNo = "no"


class ProductionRecoveryRecord(RecoveryContract):
    schema_version: Literal[1] = 1
    incident_id: str
    selected_backup_id: str
    protection_backup_id: str | None = None
    phase: RecoveryPhase = "planned"
    resources: ResourceIdentities
    authorizations: AuthorizationObservations = AuthorizationObservations()
    last_operation: str | None = None
    last_error_code: str | None = None
    last_error_at_utc: datetime | None = None
    retryable: YesNo = "no"
    verification_result: Literal["not_started", "passed", "failed"] = "not_started"
    verification_error_code: str | None = None
    monitor_stopped: TriState = "unknown"
    session_lease_free: TriState = "unknown"
    application_stopped: TriState = "unknown"
    application_connections_drained: TriState = "unknown"
    cleanup_record_id: str | None = None
    cleanup_requested: YesNo = "no"
    cleanup_completed: YesNo = "no"
    protection_backup_status: Literal["pending", "completed", "skipped_authorized"] = "pending"
    watchlist_switch_authorized: YesNo = "no"
    watchlist_was_switched: YesNo = "no"
    replacement_activated: YesNo = "no"
    monitor_first_write_observed: TriState = "unknown"
    manual_reconciliation_required: YesNo = "no"

    @field_validator("incident_id", "cleanup_record_id")
    @classmethod
    def validate_opaque_id(cls, value: str | None) -> str | None:
        if value is not None and not INCIDENT_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid opaque id")
        return value

    @field_validator("selected_backup_id", "protection_backup_id")
    @classmethod
    def validate_backup_id(cls, value: str | None) -> str | None:
        if value is not None and not BACKUP_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid canonical backup id")
        return value

    @field_validator("last_error_code", "verification_error_code")
    @classmethod
    def validate_error_code(cls, value: str | None) -> str | None:
        if value is not None and not STABLE_CODE_PATTERN.fullmatch(value):
            raise ValueError("invalid stable error code")
        return value

    @model_validator(mode="after")
    def validate_terminal_state(self) -> "ProductionRecoveryRecord":
        if self.cleanup_completed == "yes" and self.cleanup_requested != "yes":
            raise ValueError("cleanup completion requires request")
        if self.replacement_activated == "yes" and self.phase == "planned":
            raise ValueError("planned recovery cannot have activated replacement")
        if (
            self.watchlist_switch_authorized
            != self.authorizations.watchlist_switch_authorized
        ):
            raise ValueError("watchlist authorization observations disagree")
        return self


class ProductionCleanupRecord(RecoveryContract):
    schema_version: Literal[1] = 1
    cleanup_record_id: str
    incident_id: str
    replacement_database_identity: str
    replacement_identity_token: str
    expected_owner_identity: str
    phase: Literal["planned", "cleanup_started", "cleanup_completed"] = "planned"
    drop_observed: YesNo = "no"
    last_error_code: str | None = None

    @field_validator("cleanup_record_id", "incident_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not INCIDENT_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid opaque id")
        return value


FORWARD_TRANSITIONS: dict[RecoveryPhase, frozenset[RecoveryPhase]] = {
    "planned": frozenset({"protection_backup_started", "protection_backup_skipped_authorized"}),
    "protection_backup_started": frozenset({"protection_backup_completed"}),
    "protection_backup_completed": frozenset({"replacement_create_started"}),
    "protection_backup_skipped_authorized": frozenset({"replacement_create_started"}),
    "replacement_create_started": frozenset({"replacement_created"}),
    "replacement_created": frozenset({"identity_commit_started"}),
    "identity_commit_started": frozenset({"identity_committed"}),
    "identity_committed": frozenset({"restore_started"}),
    "restore_started": frozenset({"restore_completed"}),
    "restore_completed": frozenset({"verification_started"}),
    "verification_started": frozenset({"verification_completed"}),
    "verification_completed": frozenset({"services_stop_started"}),
    "services_stop_started": frozenset({"services_stopped"}),
    "services_stopped": frozenset({"config_protection_started"}),
    "config_protection_started": frozenset({"config_protection_completed"}),
    "config_protection_completed": frozenset({"env_switch_started"}),
    "env_switch_started": frozenset({"env_switched"}),
    "env_switched": frozenset({"watchlist_switch_started", "config_switched"}),
    "watchlist_switch_started": frozenset({"watchlist_switched"}),
    "watchlist_switched": frozenset({"config_switched"}),
    "config_switched": frozenset({"application_start_started"}),
    "application_start_started": frozenset({"application_started"}),
    "application_started": frozenset({"readiness_started"}),
    "readiness_started": frozenset({"readiness_passed"}),
    "readiness_passed": frozenset({"monitor_start_authorized"}),
    "monitor_start_authorized": frozenset({"monitor_start_started"}),
    "monitor_start_started": frozenset({"monitor_started"}),
    "monitor_started": frozenset({"monitor_write_observed", "completed"}),
    "monitor_write_observed": frozenset({"completed"}),
    "completed": frozenset(),
    "rollback_started": frozenset({"rollback_monitor_stopped"}),
    "rollback_monitor_stopped": frozenset({"rollback_application_stop_started"}),
    "rollback_application_stop_started": frozenset({"rollback_application_stopped"}),
    "rollback_application_stopped": frozenset({"rollback_env_started"}),
    "rollback_env_started": frozenset({"rollback_env_completed"}),
    "rollback_env_completed": frozenset(
        {"rollback_watchlist_started", "rollback_application_start_started"}
    ),
    "rollback_watchlist_started": frozenset({"rollback_watchlist_completed"}),
    "rollback_watchlist_completed": frozenset({"rollback_application_start_started"}),
    "rollback_application_start_started": frozenset({"rollback_application_started"}),
    "rollback_application_started": frozenset({"rollback_readiness_started"}),
    "rollback_readiness_started": frozenset({"rollback_readiness_passed"}),
    "rollback_readiness_passed": frozenset({"rolled_back"}),
    "rolled_back": frozenset(),
}

ROLLBACK_ENTRY_PHASES = frozenset({
    "env_switch_started", "env_switched", "watchlist_switch_started",
    "watchlist_switched", "config_switched", "application_start_started",
    "application_started", "readiness_started", "readiness_passed",
    "monitor_start_authorized",
})


def advance_production_recovery(
    record: ProductionRecoveryRecord, target: RecoveryPhase
) -> ProductionRecoveryRecord:
    if target == "rollback_started":
        if record.phase not in ROLLBACK_ENTRY_PHASES:
            raise ValueError("PRODUCTION_RECOVERY_PHASE_INVALID")
    elif target not in FORWARD_TRANSITIONS[record.phase]:
        raise ValueError("PRODUCTION_RECOVERY_PHASE_INVALID")
    if target == "verification_completed" and record.verification_result != "passed":
        raise ValueError("PRODUCTION_RECOVERY_VERIFICATION_NOT_PASSED")
    if target == "services_stopped" and {
        record.monitor_stopped, record.session_lease_free,
        record.application_stopped, record.application_connections_drained,
    } != {"yes"}:
        raise ValueError("PRODUCTION_RECOVERY_SERVICES_NOT_STOPPED")
    if target == "protection_backup_skipped_authorized" and (
        record.authorizations.protection_skip_authorized != "yes"
        or record.protection_backup_status != "skipped_authorized"
    ):
        raise ValueError("PRODUCTION_RECOVERY_SKIP_NOT_AUTHORIZED")
    if target == "protection_backup_completed" and (
        record.protection_backup_status != "completed"
        or record.protection_backup_id is None
    ):
        raise ValueError("PRODUCTION_RECOVERY_PROTECTION_BACKUP_INVALID")
    if record.phase == "env_switched":
        expected = (
            "watchlist_switch_started"
            if record.watchlist_switch_authorized == "yes"
            else "config_switched"
        )
        if target != expected:
            raise ValueError("PRODUCTION_RECOVERY_PHASE_INVALID")
    if record.phase == "rollback_env_completed":
        expected = (
            "rollback_watchlist_started"
            if record.watchlist_was_switched == "yes"
            else "rollback_application_start_started"
        )
        if target != expected:
            raise ValueError("PRODUCTION_RECOVERY_PHASE_INVALID")
    if target == "completed" and (
        record.monitor_first_write_observed == "unknown"
        or record.manual_reconciliation_required == "yes"
    ):
        raise ValueError("PRODUCTION_RECOVERY_COMPLETION_INVALID")
    return ProductionRecoveryRecord.model_validate(
        {**record.model_dump(mode="python"), "phase": target}
    )


def advance_cleanup(
    record: ProductionCleanupRecord,
    target: Literal["cleanup_started", "cleanup_completed"],
) -> ProductionCleanupRecord:
    allowed = {"planned": "cleanup_started", "cleanup_started": "cleanup_completed"}
    if allowed.get(record.phase) != target:
        raise ValueError("PRODUCTION_RECOVERY_CLEANUP_PHASE_INVALID")
    if target == "cleanup_completed" and record.drop_observed != "yes":
        raise ValueError("PRODUCTION_RECOVERY_DROP_NOT_OBSERVED")
    return ProductionCleanupRecord.model_validate(
        {**record.model_dump(mode="python"), "phase": target}
    )


def cleanup_required(record: ProductionRecoveryRecord, *, replacement_exists: bool) -> bool:
    if record.cleanup_completed == "yes":
        return replacement_exists
    return record.cleanup_requested == "yes" or (
        replacement_exists and record.phase == "rolled_back"
    )
