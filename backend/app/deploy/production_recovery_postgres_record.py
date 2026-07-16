"""Durable ownership for isolated PostgreSQL recovery rehearsals."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    create_bytes_if_absent,
    ensure_private_directory,
    open_regular,
    read_regular_exact,
)
from app.deploy.production_recovery_models import StableRecoveryLockIdentity
from app.deploy.production_recovery_postgres import (
    ObservationProvider,
    TempPostgresCapabilityIssuer,
    TempPostgresRehearsalCapability,
    TempPostgresRehearsalError,
    _make_resume_evidence,
    validate_capability,
)
from app.deploy.production_recovery_postgres_models import (
    FORWARD_PHASES,
    RUN_ID_PATTERN,
    RehearsalPhase,
    TempPostgresRehearsalRecord,
    advance_rehearsal,
)
from app.deploy.backup_models import PgConnectionSpec

MAX_RECORD_BYTES = 128 * 1024


def open_rehearsal_store_for_resume(
    *,
    root: Path,
    run_id: str,
    spec: PgConnectionSpec,
    provider: ObservationProvider,
) -> "TempPostgresRehearsalRecordStore":
    """Recover cleanup authority from a durable record, never caller identities."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_INVALID")
    from app.deploy.production_recovery_postgres import validate_temp_root

    resolved, _, _ = validate_temp_root(root, initialize=False)
    record_root = resolved / "postgres-rehearsal"
    ensure_private_directory(record_root)
    lock_path = record_root / f"{run_id}.lock"
    record_path = record_root / f"{run_id}.json"
    fd: int | None = None
    try:
        fd = open_regular(lock_path, os.O_RDWR)
        before = os.fstat(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = read_regular_exact(record_path, max_bytes=MAX_RECORD_BYTES)
        record = TempPostgresRehearsalRecord.model_validate_json(payload)
        observed_lock = StableRecoveryLockIdentity(
            resolved_device=before.st_dev,
            resolved_inode=before.st_ino,
        )
        if record.run_id != run_id or record.stable_lock_identity != observed_lock:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_INVALID")
        capability = TempPostgresCapabilityIssuer().issue_resume_cleanup(
            root=resolved,
            evidence=_make_resume_evidence(record),
            spec=spec,
            provider=provider,
        )
    except BlockingIOError as exc:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_IN_PROGRESS") from exc
    except TempPostgresRehearsalError:
        raise
    except (BackupFsError, OSError, ValueError, ValidationError) as exc:
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_INVALID") from exc
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
    return TempPostgresRehearsalRecordStore(capability)


class TempPostgresRehearsalRecordStore:
    def __init__(self, capability: TempPostgresRehearsalCapability) -> None:
        validate_capability(capability)
        self.capability = capability
        self.root = capability.root / "postgres-rehearsal"
        ensure_private_directory(self.root)
        self.lock_path = self.root / f"{capability.run_id}.lock"
        create_bytes_if_absent(self.lock_path, b"", token=secrets.token_hex(8))

    def create(self) -> TempPostgresRehearsalRecord:
        self._require_scope("initial")
        with self._locked() as identity:
            record = TempPostgresRehearsalRecord(
                run_id=self.capability.run_id,
                server_identity_digest=self.capability.server_identity_digest,
                connection_identity_digest=(
                    self.capability.connection_identity_digest
                ),
                role_oid=self.capability.role_oid,
                role_identity_digest=self.capability.role_identity_digest,
                source_database_identity=self.capability.source_database,
                source_identity_token=self.capability.source_token,
                replacement_database_identity=self.capability.replacement_database,
                replacement_identity_token=self.capability.replacement_token,
                stable_lock_identity=identity,
            )
            try:
                created = create_bytes_if_absent(
                    self.record_path,
                    self._serialize(record),
                    token=secrets.token_hex(8),
                )
            except (BackupFsError, OSError) as exc:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_RECORD_WRITE_FAILED"
                ) from exc
            if not created:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_RECORD_ALREADY_EXISTS"
                )
            return record

    def read(self) -> TempPostgresRehearsalRecord:
        with self._locked() as identity:
            record = self._read_unlocked()
            self._require_binding(record, identity)
            return record

    def read_nonblocking(self) -> TempPostgresRehearsalRecord:
        with self._locked(nonblocking=True) as identity:
            record = self._read_unlocked()
            self._require_binding(record, identity)
            return record

    def advance(
        self,
        record: TempPostgresRehearsalRecord,
        target: RehearsalPhase,
    ) -> TempPostgresRehearsalRecord:
        with self._locked(record.stable_lock_identity) as identity:
            current = self._read_unlocked()
            self._require_binding(current, identity)
            if current != record:
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_STALE")
            try:
                updated = advance_rehearsal(current, target)
                self._write_unlocked(updated)
            except ValueError as exc:
                raise TempPostgresRehearsalError(str(exc)) from exc
            return updated

    def update_facts(
        self,
        record: TempPostgresRehearsalRecord,
        **changes: object,
    ) -> TempPostgresRehearsalRecord:
        allowed = {
            "dump_identity",
            "workflow_record_id",
            "source_cleanup_status",
            "replacement_cleanup_status",
            "residue_count",
            "last_error_code",
        }
        if not changes or not set(changes) <= allowed:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_FACT_UPDATE_INVALID")
        with self._locked(record.stable_lock_identity) as identity:
            current = self._read_unlocked()
            self._require_binding(current, identity)
            if current != record:
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_STALE")
            for name in {"dump_identity", "workflow_record_id"} & changes.keys():
                before = getattr(current, name)
                if before is not None and before != changes[name]:
                    raise TempPostgresRehearsalError(
                        "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                    )
            if "dump_identity" in changes and current.phase != "dump_started":
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                )
            if "workflow_record_id" in changes and current.phase not in {
                "dump_committed",
                "replacement_bound",
            }:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                )
            cleanup_fields = {
                "source_cleanup_status",
                "replacement_cleanup_status",
            }
            for name in cleanup_fields & changes.keys():
                before = getattr(current, name)
                after = changes[name]
                if before != "pending" and before != after:
                    raise TempPostgresRehearsalError(
                        "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                    )
                if after == "manual_reconciliation":
                    raise TempPostgresRehearsalError(
                        "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                    )
            if (
                changes.get("source_cleanup_status") == "completed"
                and current.phase != "source_cleanup_started"
            ):
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                )
            if (
                changes.get("replacement_cleanup_status") == "completed"
                and current.phase
                not in {
                    "workflow_terminal",
                    "source_cleanup_started",
                    "source_cleanup_completed",
                }
            ):
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                )
            if "residue_count" in changes and current.phase not in {
                "source_cleanup_started",
                "source_cleanup_completed",
            }:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                )
            if (
                "residue_count" in changes
                and current.residue_count is not None
                and current.residue_count != changes["residue_count"]
            ):
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                )
            try:
                updated = TempPostgresRehearsalRecord.model_validate(
                    {**current.model_dump(mode="python"), **changes}
                )
                self._write_unlocked(updated)
            except (ValueError, ValidationError) as exc:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                ) from exc
            return updated

    def mark_manual_reconciliation(
        self,
        record: TempPostgresRehearsalRecord,
        *,
        source: bool = False,
        replacement: bool = False,
        error_code: str = "PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED",
    ) -> TempPostgresRehearsalRecord:
        if not source and not replacement:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_FACT_UPDATE_INVALID")
        with self._locked(record.stable_lock_identity) as identity:
            current = self._read_unlocked()
            self._require_binding(current, identity)
            if current != record:
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_STALE")
            if "manual_reconciliation_required" not in FORWARD_PHASES[current.phase]:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_PHASE_INVALID"
                )
            changes: dict[str, object] = {
                "phase": "manual_reconciliation_required",
                "last_error_code": error_code,
            }
            if source:
                changes["source_cleanup_status"] = "manual_reconciliation"
            if replacement:
                changes["replacement_cleanup_status"] = "manual_reconciliation"
            try:
                updated = TempPostgresRehearsalRecord.model_validate(
                    {**current.model_dump(mode="python"), **changes}
                )
                self._write_unlocked(updated)
            except (ValueError, ValidationError) as exc:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_FACT_UPDATE_INVALID"
                ) from exc
            return updated

    def issue_resume_cleanup(
        self,
        *,
        spec: PgConnectionSpec,
        provider: ObservationProvider,
    ) -> TempPostgresRehearsalCapability:
        with self._locked(nonblocking=True) as identity:
            record = self._read_unlocked()
            self._require_binding(record, identity)
            return TempPostgresCapabilityIssuer().issue_resume_cleanup(
                root=self.capability.root,
                evidence=_make_resume_evidence(record),
                spec=spec,
                provider=provider,
            )

    @property
    def record_path(self) -> Path:
        return self.root / f"{self.capability.run_id}.json"

    def _require_scope(self, scope: str) -> None:
        validate_capability(self.capability)
        if self.capability.scope != scope:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")

    def _require_binding(
        self,
        record: TempPostgresRehearsalRecord,
        lock_identity: StableRecoveryLockIdentity,
    ) -> None:
        validate_capability(self.capability)
        if (
            record.run_id != self.capability.run_id
            or record.server_identity_digest != self.capability.server_identity_digest
            or record.connection_identity_digest
            != self.capability.connection_identity_digest
            or record.role_oid != self.capability.role_oid
            or record.role_identity_digest != self.capability.role_identity_digest
            or record.source_database_identity != self.capability.source_database
            or record.source_identity_token != self.capability.source_token
            or record.replacement_database_identity
            != self.capability.replacement_database
            or record.replacement_identity_token != self.capability.replacement_token
            or record.stable_lock_identity != lock_identity
        ):
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_INVALID")

    def _read_unlocked(self) -> TempPostgresRehearsalRecord:
        try:
            return TempPostgresRehearsalRecord.model_validate_json(
                read_regular_exact(self.record_path, max_bytes=MAX_RECORD_BYTES)
            )
        except (BackupFsError, OSError, ValueError, ValidationError) as exc:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_RECORD_INVALID"
            ) from exc

    def _write_unlocked(self, record: TempPostgresRehearsalRecord) -> None:
        try:
            atomic_write_bytes(
                self.record_path,
                self._serialize(record),
                token=secrets.token_hex(8),
            )
        except (BackupFsError, OSError) as exc:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_RECORD_WRITE_FAILED"
            ) from exc

    @staticmethod
    def _serialize(record: TempPostgresRehearsalRecord) -> bytes:
        return (
            json.dumps(
                record.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )

    @contextmanager
    def _locked(
        self,
        expected: StableRecoveryLockIdentity | None = None,
        *,
        nonblocking: bool = False,
    ) -> Iterator[StableRecoveryLockIdentity]:
        validate_capability(self.capability)
        fd: int | None = None
        try:
            fd = open_regular(self.lock_path, os.O_RDWR)
            before = os.fstat(fd)
            observed = StableRecoveryLockIdentity(
                resolved_device=before.st_dev,
                resolved_inode=before.st_ino,
            )
            if not stat.S_ISREG(before.st_mode) or (
                expected is not None and observed != expected
            ):
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_LOCK_INVALID")
            operation = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
            try:
                fcntl.flock(fd, operation)
            except BlockingIOError as exc:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_IN_PROGRESS"
                ) from exc
            after = os.fstat(fd)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_LOCK_INVALID")
            yield observed
        except TempPostgresRehearsalError:
            raise
        except (BackupFsError, OSError) as exc:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_LOCK_INVALID") from exc
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
