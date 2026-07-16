"""Temp-only durable record storage for production-recovery rehearsals."""

from __future__ import annotations

import fcntl
import json
import os
import pickle
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    create_bytes_if_absent,
    ensure_private_directory,
    open_regular,
    read_regular_exact,
)
from app.deploy.production_recovery_models import (
    ProductionCleanupRecord,
    ProductionRecoveryRecord,
    RecoveryPhase,
    StableRecoveryLockIdentity,
    advance_cleanup,
    advance_production_recovery,
)

MAX_RECORD_BYTES = 128 * 1024
_CAPABILITY_SECRET = object()
_PROCESS_NONCE = secrets.token_hex(16)


class ProductionRecoveryError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class RecoveryOperationLease:
    def __init__(
        self,
        store: "TempRecoveryRecordStore",
        current: ProductionRecoveryRecord,
    ) -> None:
        self._store = store
        self.current = current

    def advance(self, target: RecoveryPhase) -> ProductionRecoveryRecord:
        try:
            updated = advance_production_recovery(self.current, target)
            self._store._write_unlocked(updated)
        except ValueError as exc:
            raise ProductionRecoveryError(str(exc)) from exc
        except (BackupFsError, OSError) as exc:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
            ) from exc
        self.current = updated
        return updated

    def update_facts(self, **changes: object) -> ProductionRecoveryRecord:
        self.current = self._store._update_facts_unlocked(self.current, changes)
        return self.current


class TempRecoveryCapability:
    __slots__ = ("root", "device", "inode", "process_nonce", "purpose", "_secret")

    def __init__(self, root: Path, secret: object) -> None:
        if secret is not _CAPABILITY_SECRET:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")
        info = root.lstat()
        self.root = root.resolve()
        self.device = info.st_dev
        self.inode = info.st_ino
        self.process_nonce = _PROCESS_NONCE
        self.purpose = "production_recovery_rehearsal"
        self._secret = secret

    def __reduce__(self) -> object:
        raise pickle.PicklingError("TempRecoveryCapability is not serializable")


def create_temp_recovery_capability(root: Path) -> TempRecoveryCapability:
    resolved = root.expanduser().resolve(strict=False)
    temp_root = Path(tempfile.gettempdir()).resolve()
    forbidden = ((Path.home() / ".tg-hub").resolve(),)
    if (resolved != temp_root and temp_root not in resolved.parents) or any(
        resolved == item or item in resolved.parents for item in forbidden
    ):
        raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")
    ensure_private_directory(resolved)
    return TempRecoveryCapability(resolved, _CAPABILITY_SECRET)


class TempRecoveryRecordStore:
    def __init__(self, capability: TempRecoveryCapability) -> None:
        self.capability = capability
        self._validate_capability()
        self.root = capability.root / "production-recovery"
        ensure_private_directory(self.root)
        self.lock_path = self.root / "production-recovery.lock"
        create_bytes_if_absent(self.lock_path, b"", token=secrets.token_hex(8))

    def _validate_capability(self) -> None:
        info = self.capability.root.lstat()
        if (
            self.capability._secret is not _CAPABILITY_SECRET
            or self.capability.process_nonce != _PROCESS_NONCE
            or self.capability.purpose != "production_recovery_rehearsal"
            or info.st_dev != self.capability.device
            or info.st_ino != self.capability.inode
            or self.capability.root.is_symlink()
        ):
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")

    def create(self, record: ProductionRecoveryRecord) -> ProductionRecoveryRecord:
        self._validate_capability()
        if record.stable_lock_identity is not None:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_INVALID")
        with self._locked() as lock_identity:
            persisted = ProductionRecoveryRecord.model_validate(
                {
                    **record.model_dump(mode="python"),
                    "stable_lock_identity": lock_identity.model_dump(),
                }
            )
            try:
                created = create_bytes_if_absent(
                    self._path(persisted.incident_id),
                    self._serialize(persisted),
                    token=os.urandom(8).hex(),
                )
                if not created:
                    raise ProductionRecoveryError(
                        "PRODUCTION_RECOVERY_RECORD_ALREADY_EXISTS"
                    )
            except (BackupFsError, OSError, ValueError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
                ) from exc
            return persisted

    def read(self, incident_id: str) -> ProductionRecoveryRecord:
        self._validate_capability()
        with self._locked() as lock_identity:
            try:
                record = self._read_unlocked(incident_id)
                self._require_lock_identity(record, lock_identity)
                return record
            except (BackupFsError, OSError, ValueError, ValidationError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_INVALID"
                ) from exc

    def advance(
        self, record: ProductionRecoveryRecord, target: RecoveryPhase
    ) -> ProductionRecoveryRecord:
        self._validate_capability()
        with self._locked(record.stable_lock_identity) as lock_identity:
            current = self._read_main_for_update(record.incident_id)
            self._require_lock_identity(current, lock_identity)
            if current != record:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            try:
                updated = advance_production_recovery(current, target)
            except ValueError as exc:
                raise ProductionRecoveryError(str(exc)) from exc
            try:
                atomic_write_bytes(
                    self._path(updated.incident_id),
                    self._serialize(updated),
                    token=os.urandom(8).hex(),
                )
            except (BackupFsError, OSError, ValueError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
                ) from exc
            return updated

    def update_facts(
        self, record: ProductionRecoveryRecord, **changes: object
    ) -> ProductionRecoveryRecord:
        allowed = {
            "protection_backup_id",
            "protection_manifest_sha256",
            "protection_database_dump_sha256",
            "protection_watchlist_sha256",
            "resources",
            "authorizations",
            "last_operation",
            "last_error_code",
            "last_error_at_utc",
            "retryable",
            "verification_result",
            "verification_error_code",
            "monitor_stopped",
            "session_lease_free",
            "application_stopped",
            "application_connections_drained",
            "cleanup_record_id",
            "cleanup_requested",
            "cleanup_completed",
            "protection_backup_status",
            "watchlist_switch_authorized",
            "watchlist_was_switched",
            "replacement_activated",
            "monitor_first_write_observed",
            "monitor_generation_id",
            "monitor_write_baseline",
            "monitor_first_write_observed_at_utc",
            "manual_reconciliation_required",
        }
        if not changes or not set(changes) <= allowed:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_FACT_UPDATE_INVALID")
        self._validate_capability()
        with self._locked(record.stable_lock_identity) as lock_identity:
            current = self._read_main_for_update(record.incident_id)
            self._require_lock_identity(current, lock_identity)
            if current != record:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            return self._update_facts_unlocked(current, changes)

    def _update_facts_unlocked(
        self, current: ProductionRecoveryRecord, changes: dict[str, object]
    ) -> ProductionRecoveryRecord:
        allowed = {
            "protection_backup_id",
            "protection_manifest_sha256",
            "protection_database_dump_sha256",
            "protection_watchlist_sha256",
            "resources",
            "authorizations",
            "last_operation",
            "last_error_code",
            "last_error_at_utc",
            "retryable",
            "verification_result",
            "verification_error_code",
            "monitor_stopped",
            "session_lease_free",
            "application_stopped",
            "application_connections_drained",
            "cleanup_record_id",
            "cleanup_requested",
            "cleanup_completed",
            "protection_backup_status",
            "watchlist_switch_authorized",
            "watchlist_was_switched",
            "replacement_activated",
            "monitor_first_write_observed",
            "monitor_generation_id",
            "monitor_write_baseline",
            "monitor_first_write_observed_at_utc",
            "manual_reconciliation_required",
        }
        if not changes or not set(changes) <= allowed:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_FACT_UPDATE_INVALID")
        if (
            current.replacement_activated == "yes"
            and changes.get("replacement_activated") == "no"
        ):
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
            )
        self._validate_fact_update(current, changes)
        if "resources" in changes:
            self._validate_resource_update(current, changes["resources"])
        if (
            changes.get("cleanup_completed") == "yes"
            and current.cleanup_completed != "yes"
        ):
            self._require_terminal_cleanup_child(current)
        try:
            updated = ProductionRecoveryRecord.model_validate(
                {**current.model_dump(mode="python"), **changes}
            )
        except (ValueError, ValidationError) as exc:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
            ) from exc
        try:
            self._write_unlocked(updated)
        except ProductionRecoveryError:
            raise
        except (BackupFsError, OSError) as exc:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
            ) from exc
        return updated

    def _require_terminal_cleanup_child(
        self, current: ProductionRecoveryRecord
    ) -> None:
        if current.cleanup_record_id is None:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_IDENTITY_INVALID"
            )
        child = self._read_cleanup_for_update(current.cleanup_record_id)
        self._require_cleanup_binding(current, child)
        if child.phase != "cleanup_completed" or child.drop_observed != "yes":
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_NOT_COMPLETED"
            )

    @staticmethod
    def _validate_fact_update(
        current: ProductionRecoveryRecord, changes: dict[str, object]
    ) -> None:
        fill_once = {
            "protection_backup_id",
            "protection_manifest_sha256",
            "protection_database_dump_sha256",
            "protection_watchlist_sha256",
            "cleanup_record_id",
            "monitor_generation_id",
            "monitor_write_baseline",
            "monitor_first_write_observed_at_utc",
        }
        for name in fill_once & changes.keys():
            before = getattr(current, name)
            after = changes[name]
            if before is not None and before != after:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                )

        sticky_yes = {
            "monitor_stopped",
            "session_lease_free",
            "application_stopped",
            "application_connections_drained",
            "cleanup_requested",
            "cleanup_completed",
            "watchlist_was_switched",
            "replacement_activated",
            "manual_reconciliation_required",
        }
        for name in sticky_yes & changes.keys():
            if getattr(current, name) == "yes" and changes[name] != "yes":
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                )

        if "authorizations" in changes:
            try:
                candidate = type(current.authorizations).model_validate(
                    changes["authorizations"]
                )
            except (ValueError, ValidationError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                ) from exc
            for name in type(current.authorizations).model_fields:
                if (
                    getattr(current.authorizations, name) == "yes"
                    and getattr(candidate, name) != "yes"
                ):
                    raise ProductionRecoveryError(
                        "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                    )

        if "protection_backup_status" in changes:
            after = changes["protection_backup_status"]
            if current.protection_backup_status != "pending" and (
                after != current.protection_backup_status
            ):
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                )

        if "verification_result" in changes:
            after = changes["verification_result"]
            if (
                current.phase != "verification_started"
                or current.verification_result != "not_started"
                or after not in {"passed", "failed"}
            ):
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                )

        if "monitor_first_write_observed" in changes:
            after = changes["monitor_first_write_observed"]
            if (
                current.phase not in {"monitor_started", "monitor_write_observed"}
                or current.monitor_first_write_observed != "unknown"
                or after not in {"yes", "no"}
            ):
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                )

        monitor_setup_fields = {
            "monitor_generation_id",
            "monitor_write_baseline",
        }
        if monitor_setup_fields & changes.keys() and current.phase not in {
            "readiness_passed",
            "monitor_start_authorized",
        }:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
            )
        if "monitor_first_write_observed_at_utc" in changes and (
            current.phase not in {"monitor_started", "monitor_write_observed"}
            or changes.get("monitor_first_write_observed", "unknown") == "unknown"
        ):
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
            )

    @staticmethod
    def _validate_resource_update(
        current: ProductionRecoveryRecord, candidate_value: object
    ) -> None:
        try:
            candidate = type(current.resources).model_validate(candidate_value)
        except (ValueError, ValidationError) as exc:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
            ) from exc
        fill_once = {
            "protected_env_sha256",
            "staged_watchlist_sha256",
            "protected_watchlist_sha256",
        }
        for name in type(current.resources).model_fields:
            before = getattr(current.resources, name)
            after = getattr(candidate, name)
            if name not in fill_once and before != after:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                )
            if name in fill_once and before is not None and before != after:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_FACT_UPDATE_INVALID"
                )

    def create_cleanup(
        self, main: ProductionRecoveryRecord, child: ProductionCleanupRecord
    ) -> None:
        if (
            main.cleanup_requested != "yes"
            or main.cleanup_record_id != child.cleanup_record_id
            or child.incident_id != main.incident_id
            or child.stable_lock_identity != main.stable_lock_identity
        ):
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_IDENTITY_INVALID"
            )
        self._require_cleanup_binding(main, child)
        with self._locked(main.stable_lock_identity) as lock_identity:
            current = self._read_main_for_update(main.incident_id)
            self._require_lock_identity(current, lock_identity)
            if current != main:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            try:
                created = create_bytes_if_absent(
                    self._cleanup_path(child.cleanup_record_id),
                    self._serialize_model(child),
                    token=os.urandom(8).hex(),
                )
            except (BackupFsError, OSError, ValueError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
                ) from exc
            if not created:
                existing = self._read_cleanup_for_update(child.cleanup_record_id)
                if existing != child:
                    raise ProductionRecoveryError(
                        "PRODUCTION_RECOVERY_CLEANUP_IDENTITY_INVALID"
                    )

    def read_cleanup(
        self, main: ProductionRecoveryRecord
    ) -> ProductionCleanupRecord:
        if main.cleanup_record_id is None:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_IDENTITY_INVALID"
            )
        with self._locked(main.stable_lock_identity) as lock_identity:
            current_main = self._read_main_for_update(main.incident_id)
            self._require_lock_identity(current_main, lock_identity)
            if current_main != main:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            child = self._read_cleanup_for_update(main.cleanup_record_id)
            self._require_cleanup_binding(current_main, child)
            return child

    def advance_cleanup(
        self,
        main: ProductionRecoveryRecord,
        child: ProductionCleanupRecord,
        target: Literal["cleanup_started", "cleanup_completed"],
    ) -> ProductionCleanupRecord:
        with self._locked(main.stable_lock_identity) as lock_identity:
            current_main = self._read_main_for_update(main.incident_id)
            self._require_lock_identity(current_main, lock_identity)
            current_child = self._read_cleanup_for_update(child.cleanup_record_id)
            self._require_cleanup_binding(current_main, current_child)
            if current_main != main or current_child != child:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            try:
                updated = advance_cleanup(current_child, target)
                atomic_write_bytes(
                    self._cleanup_path(updated.cleanup_record_id),
                    self._serialize_model(updated),
                    token=os.urandom(8).hex(),
                )
            except ValueError as exc:
                raise ProductionRecoveryError(str(exc)) from exc
            except (BackupFsError, OSError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
                ) from exc
            return updated

    def update_cleanup_facts(
        self,
        main: ProductionRecoveryRecord,
        child: ProductionCleanupRecord,
        **changes: object,
    ) -> ProductionCleanupRecord:
        if not changes or not set(changes) <= {"drop_observed", "last_error_code"}:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_FACT_UPDATE_INVALID"
            )
        with self._locked(main.stable_lock_identity) as lock_identity:
            current_main = self._read_main_for_update(main.incident_id)
            self._require_lock_identity(current_main, lock_identity)
            current_child = self._read_cleanup_for_update(child.cleanup_record_id)
            self._require_cleanup_binding(current_main, current_child)
            if current_main != main or current_child != child:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            if "drop_observed" in changes and (
                current_child.phase != "cleanup_started"
                or current_child.drop_observed != "no"
                or changes["drop_observed"] != "yes"
            ):
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_CLEANUP_FACT_UPDATE_INVALID"
                )
            try:
                updated = ProductionCleanupRecord.model_validate(
                    {**current_child.model_dump(mode="python"), **changes}
                )
                atomic_write_bytes(
                    self._cleanup_path(updated.cleanup_record_id),
                    self._serialize_model(updated),
                    token=os.urandom(8).hex(),
                )
            except (BackupFsError, OSError, ValueError, ValidationError) as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_RECORD_WRITE_FAILED"
                ) from exc
            return updated

    @staticmethod
    def _serialize(record: ProductionRecoveryRecord) -> bytes:
        return TempRecoveryRecordStore._serialize_model(record)

    @staticmethod
    def _serialize_model(record: object) -> bytes:
        return (
            json.dumps(
                record.model_dump(mode="json"),  # type: ignore[attr-defined]
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )

    def _read_unlocked(self, incident_id: str) -> ProductionRecoveryRecord:
        return ProductionRecoveryRecord.model_validate_json(
            read_regular_exact(self._path(incident_id), max_bytes=MAX_RECORD_BYTES)
        )

    def _read_main_for_update(self, incident_id: str) -> ProductionRecoveryRecord:
        try:
            return self._read_unlocked(incident_id)
        except (BackupFsError, OSError, ValueError, ValidationError) as exc:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_RECORD_INVALID"
            ) from exc

    def _read_cleanup_unlocked(
        self, cleanup_record_id: str
    ) -> ProductionCleanupRecord:
        return ProductionCleanupRecord.model_validate_json(
            read_regular_exact(
                self._cleanup_path(cleanup_record_id), max_bytes=MAX_RECORD_BYTES
            )
        )

    def _read_cleanup_for_update(
        self, cleanup_record_id: str
    ) -> ProductionCleanupRecord:
        try:
            return self._read_cleanup_unlocked(cleanup_record_id)
        except (BackupFsError, OSError, ValueError, ValidationError) as exc:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_RECORD_INVALID"
            ) from exc

    @staticmethod
    def _require_lock_identity(
        record: ProductionRecoveryRecord,
        observed: StableRecoveryLockIdentity,
    ) -> None:
        if record.stable_lock_identity != observed:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_LOCK_INVALID")

    @staticmethod
    def _require_cleanup_binding(
        main: ProductionRecoveryRecord, child: ProductionCleanupRecord
    ) -> None:
        if (
            main.cleanup_record_id != child.cleanup_record_id
            or main.incident_id != child.incident_id
            or main.stable_lock_identity != child.stable_lock_identity
            or main.resources.replacement_database_identity
            != child.replacement_database_identity
            or main.resources.replacement_identity_token
            != child.replacement_identity_token
            or main.resources.expected_owner_identity
            != child.expected_owner_identity
        ):
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_IDENTITY_INVALID"
            )

    def _path(self, incident_id: str) -> Path:
        if len(incident_id) != 32 or any(c not in "0123456789abcdef" for c in incident_id):
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_INVALID")
        return self.root / f"{incident_id}.json"

    def _cleanup_path(self, cleanup_record_id: str) -> Path:
        if len(cleanup_record_id) != 32 or any(
            char not in "0123456789abcdef" for char in cleanup_record_id
        ):
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_IDENTITY_INVALID"
            )
        return self.root / f"cleanup-{cleanup_record_id}.json"

    def _write_unlocked(self, record: ProductionRecoveryRecord) -> None:
        atomic_write_bytes(
            self._path(record.incident_id),
            self._serialize(record),
            token=os.urandom(8).hex(),
        )

    @contextmanager
    def operation_lease(
        self, record: ProductionRecoveryRecord
    ) -> Iterator[RecoveryOperationLease]:
        self._validate_capability()
        with self._locked(
            record.stable_lock_identity, nonblocking=True
        ) as lock_identity:
            current = self._read_main_for_update(record.incident_id)
            self._require_lock_identity(current, lock_identity)
            if current != record:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_STALE")
            yield RecoveryOperationLease(self, current)

    @contextmanager
    def _locked(
        self,
        expected: StableRecoveryLockIdentity | None = None,
        *,
        nonblocking: bool = False,
    ) -> Iterator[StableRecoveryLockIdentity]:
        try:
            fd = open_regular(self.lock_path, os.O_RDWR)
            info = os.fstat(fd)
            observed = StableRecoveryLockIdentity(
                resolved_device=info.st_dev, resolved_inode=info.st_ino
            )
            if not stat.S_ISREG(info.st_mode) or (
                expected is not None and observed != expected
            ):
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_LOCK_INVALID"
                )
            operation = fcntl.LOCK_EX
            if nonblocking:
                operation |= fcntl.LOCK_NB
            try:
                fcntl.flock(fd, operation)
            except BlockingIOError as exc:
                raise ProductionRecoveryError(
                    "PRODUCTION_RECOVERY_IN_PROGRESS"
                ) from exc
            after = os.fstat(fd)
            if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_LOCK_INVALID")
            yield observed
        except ProductionRecoveryError:
            raise
        except (BackupFsError, OSError) as exc:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_LOCK_INVALID") from exc
        finally:
            if "fd" in locals():
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)


class ProductionRecoveryAdapter:
    """Permanent fail-closed boundary until a later production gate authorizes it."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise ProductionRecoveryError("PRODUCTION_RECOVERY_NOT_AUTHORIZED")
