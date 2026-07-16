"""Deterministic backup retention planning and temp-only mutation gates."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import ctypes
import errno
import sys
import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal

from pydantic import Field, ValidationError

from app.config import Settings
from app.config import load_settings
from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    backup_root_lock,
    create_bytes_if_absent,
    ensure_private_directory,
    read_regular_exact,
    stream_sha256,
    unlink_durable,
    validate_backup_root,
)
from app.deploy.backup_inventory import BackupInventoryService, MAX_SIDECAR_BYTES
from app.deploy.backup_models import (
    BACKUP_ID_PATTERN,
    OPAQUE_ID_PATTERN,
    BackupManifest,
    BackupPinSidecar,
    ContractModel,
)
from app.deploy.backup_verify import MAX_MANIFEST_BYTES

UTC = timezone.utc
POLICY_NAMESPACE = "tg-hub-backup-retention"
POLICY_VERSION = 1
SELECTION_VERSION = 1
PLAN_SCHEMA_VERSION = 1
AUDIT_SCHEMA_VERSION = 1


class RetentionError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class RetentionPolicy(ContractModel):
    daily_slots: int = Field(default=7, ge=0, le=366)
    weekly_slots: int = Field(default=4, ge=0, le=53)
    minimum_valid_packages: int = Field(default=2, ge=1)
    archive_budget_bytes: int = Field(default=5 * 1024**3, ge=1)


class RetentionPackageIdentity(ContractModel):
    backup_id: str
    created_at_utc: datetime
    manifest_sha256: str
    database_dump_sha256: str
    database_dump_size: int = Field(ge=0)
    watchlist_snapshot_sha256: str
    watchlist_snapshot_size: int = Field(ge=0)
    package_bytes: int = Field(ge=0)
    verification_identity: str
    pin_identity: str
    hold_identity: str
    restore_verified: bool
    pinned: bool
    recovery_held: bool


class RetentionPlan(ContractModel):
    schema_version: Literal[1] = 1
    plan_id: str
    created_at_utc: datetime
    selection_reference_at_utc: datetime
    policy_namespace: Literal["tg-hub-backup-retention"] = POLICY_NAMESPACE
    policy_version: Literal[1] = POLICY_VERSION
    selection_algorithm_version: Literal[1] = SELECTION_VERSION
    root_device: int
    root_inode: int
    canonical_inventory_snapshot_digest: str
    packages: tuple[RetentionPackageIdentity, ...]
    daily_winners: tuple[str, ...]
    weekly_winners: tuple[str, ...]
    minimum_protected: tuple[str, ...]
    pinned_protected: tuple[str, ...]
    held_protected: tuple[str, ...]
    verified_floor_backup_id: str
    protected_backup_ids: tuple[str, ...]
    candidate_backup_ids: tuple[str, ...]
    budget_status: Literal[
        "within_budget",
        "exceeded_recoverable_after_plan",
        "exceeded_unrecoverable_after_plan",
    ]
    total_package_bytes: int = Field(ge=0)
    post_plan_package_bytes: int = Field(ge=0)


class RetentionPlanResult(ContractModel):
    status: Literal["pass", "fail"]
    plan: RetentionPlan | None = None
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"


class RetentionPlanReport(ContractModel):
    status: Literal["pass", "fail"]
    plan_id: str | None
    selection_reference_at_utc: datetime | None
    protected_backup_ids: tuple[str, ...]
    candidate_backup_ids: tuple[str, ...]
    budget_status: str | None
    total_package_bytes: int | None
    post_plan_package_bytes: int | None
    error_code: str | None
    report_desensitized: Literal["yes"] = "yes"


class PinMutationResult(ContractModel):
    status: Literal["pass", "fail"]
    backup_id: str | None
    disposition: Literal[
        "created", "removed", "idempotent", "rejected", "unknown"
    ]
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"


class PinAuditJournal(ContractModel):
    schema_version: Literal[1] = AUDIT_SCHEMA_VERSION
    operation_id: str
    operation: Literal["pin", "unpin"]
    backup_id: str
    requested_reason_code: str | None
    observed_before_identity: str
    result_identity: str
    intended_pin: BackupPinSidecar | None
    intended_result: Literal["created", "removed", "idempotent", "rejected"]
    phase: Literal["planned", "mutation_started", "mutation_committed", "completed"]
    result: Literal["created", "removed", "idempotent", "rejected"] | None
    error_code: str | None
    completed_at_utc: datetime | None


class RetentionCandidateProgress(ContractModel):
    backup_id: str
    phase: Literal[
        "planned", "rename_started", "pending", "delete_started",
        "deleted", "cleanup_required",
    ] = "planned"
    file_index: int = Field(default=0, ge=0, le=3)
    file_phase: Literal["ready", "unlink_started"] = "ready"
    directory_remove_phase: Literal["ready", "rmdir_started", "completed"] = "ready"


class RetentionExecutionJournal(ContractModel):
    schema_version: Literal[1] = 1
    plan_id: str
    candidates: tuple[RetentionCandidateProgress, ...]


class RetentionApplyResult(ContractModel):
    status: Literal["pass", "fail"]
    plan_id: str
    deleted_backup_ids: tuple[str, ...]
    cleanup_required_backup_ids: tuple[str, ...]
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"


_CAPABILITY_SECRET = object()


class TempMutationCapability:
    __slots__ = ("root", "device", "inode", "_secret")

    def __init__(self, root: Path, secret: object) -> None:
        info = root.lstat()
        self.root = root.resolve()
        self.device = info.st_dev
        self.inode = info.st_ino
        self._secret = secret


def create_temp_mutation_capability(root: Path) -> TempMutationCapability:
    validate_backup_root(root)
    resolved = root.resolve()
    home_backup = (Path.home() / ".tg-hub" / "backups").resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if (
        resolved == home_backup
        or home_backup in resolved.parents
        or (resolved != temp_root and temp_root not in resolved.parents)
    ):
        raise RetentionError("BACKUP_PIN_WRITE_NOT_AUTHORIZED")
    return TempMutationCapability(root, _CAPABILITY_SECRET)


class RetentionPlanService:
    def __init__(
        self,
        settings: Settings,
        *,
        inventory: BackupInventoryService | None = None,
        policy: RetentionPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.inventory = inventory or BackupInventoryService(settings)
        self.policy = policy or RetentionPolicy()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.plan_root = settings.BACKUP_DIR.expanduser().parent / "runtime" / "retention-plans"

    async def plan(self) -> RetentionPlanResult:
        root = self.settings.BACKUP_DIR.expanduser()
        try:
            validate_backup_root(root)
            with backup_root_lock(root, mode="shared"):
                observed = await self.inventory._inventory_locked(root)
                if (
                    observed.status != "pass"
                    or observed.manual_review_count
                    or observed.unrecognized_entry_count
                    or _has_entries(root / ".retention-pending")
                    or _has_unfinished_pin_audit(root / ".pin-audit")
                ):
                    raise RetentionError("BACKUP_RETENTION_INVENTORY_UNSAFE")
                reference = _utc(self.clock())
                packages = tuple(
                    sorted(
                        (_package_identity(root, item.backup_id) for item in observed.entries),
                        key=lambda item: item.backup_id,
                    )
                )
                plan = _select(packages, self.policy, reference, root.lstat())
                self._write_plan(plan)
                if plan.budget_status == "exceeded_unrecoverable_after_plan":
                    return RetentionPlanResult(
                        status="fail", plan=plan,
                        error_code="BACKUP_BUDGET_EXCEEDED_UNRECOVERABLE",
                    )
                return RetentionPlanResult(status="pass", plan=plan)
        except (BackupFsError, RetentionError, OSError, ValidationError) as exc:
            return RetentionPlanResult(
                status="fail",
                error_code=getattr(exc, "error_code", "BACKUP_RETENTION_INVENTORY_UNSAFE"),
            )

    def _write_plan(self, plan: RetentionPlan) -> None:
        ensure_private_directory(self.plan_root)
        path = self.plan_root / f"{plan.plan_id}.json"
        payload = _canonical_json(plan.model_dump(mode="json"))
        if not create_bytes_if_absent(path, payload, token=secrets.token_hex(8)):
            raise RetentionError("BACKUP_RETENTION_INVENTORY_UNSAFE")


class PinMutationService:
    def __init__(
        self,
        settings: Settings,
        capability: TempMutationCapability,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.capability = capability
        self.clock = clock or (lambda: datetime.now(UTC))

    def pin(self, backup_id: str, reason_code: str) -> PinMutationResult:
        return self._mutate("pin", backup_id, reason_code)

    def unpin(self, backup_id: str) -> PinMutationResult:
        return self._mutate("unpin", backup_id, None)

    def reconcile(self, operation_id: str) -> PinMutationResult:
        root = self.settings.BACKUP_DIR.expanduser()
        if not OPAQUE_ID_PATTERN.fullmatch(operation_id):
            return PinMutationResult(
                status="fail", backup_id=None, disposition="rejected",
                error_code="BACKUP_PIN_INVALID",
            )
        try:
            _require_capability(root, self.capability, "BACKUP_PIN_WRITE_NOT_AUTHORIZED")
            with backup_root_lock(root, mode="exclusive"):
                path = root / ".pin-audit" / f"{operation_id}.json"
                journal = PinAuditJournal.model_validate_json(
                    read_regular_exact(path, max_bytes=MAX_SIDECAR_BYTES)
                )
                pin_path = root / ".pins" / f"{journal.backup_id}.json"
                _, current_identity = _read_pin(pin_path, journal.backup_id)
                if journal.phase == "completed":
                    return PinMutationResult(
                        status="pass", backup_id=journal.backup_id,
                        disposition=journal.result or journal.intended_result,
                    )
                if journal.phase == "planned":
                    if current_identity != journal.observed_before_identity:
                        raise RetentionError("BACKUP_PIN_INVALID")
                    _write_journal(
                        path, journal, phase="completed", result="rejected",
                        error_code=None, completed_at_utc=_utc(self.clock()),
                    )
                    return PinMutationResult(
                        status="pass", backup_id=journal.backup_id,
                        disposition="rejected",
                    )
                if journal.phase == "mutation_started":
                    if current_identity == journal.observed_before_identity:
                        if journal.operation == "pin":
                            if journal.intended_pin is None:
                                raise RetentionError("BACKUP_PIN_INVALID")
                            payload = _canonical_json(
                                journal.intended_pin.model_dump(mode="json")
                            )
                            if not create_bytes_if_absent(
                                pin_path, payload, token=operation_id
                            ):
                                raise RetentionError("BACKUP_PIN_INVALID")
                        elif current_identity != "missing":
                            unlink_durable(pin_path)
                        _, current_identity = _read_pin(pin_path, journal.backup_id)
                    if current_identity != journal.result_identity:
                        raise RetentionError("BACKUP_PIN_INVALID")
                    journal = _write_journal(
                        path, journal, phase="mutation_committed"
                    )
                if journal.phase == "mutation_committed":
                    _, current_identity = _read_pin(pin_path, journal.backup_id)
                    if current_identity != journal.result_identity:
                        raise RetentionError("BACKUP_PIN_INVALID")
                    _write_journal(
                        path, journal, phase="completed",
                        result=journal.intended_result,
                        completed_at_utc=_utc(self.clock()),
                    )
                return PinMutationResult(
                    status="pass", backup_id=journal.backup_id,
                    disposition=journal.intended_result,
                )
        except (BackupFsError, RetentionError, OSError, ValidationError) as exc:
            return PinMutationResult(
                status="fail", backup_id=None, disposition="rejected",
                error_code=getattr(exc, "error_code", "BACKUP_PIN_INVALID"),
            )

    def _mutate(
        self, operation: Literal["pin", "unpin"], backup_id: str, reason: str | None
    ) -> PinMutationResult:
        root = self.settings.BACKUP_DIR.expanduser()
        try:
            _require_capability(root, self.capability, "BACKUP_PIN_WRITE_NOT_AUTHORIZED")
            if not BACKUP_ID_PATTERN.fullmatch(backup_id):
                raise RetentionError("BACKUP_PIN_INVALID")
            with backup_root_lock(root, mode="exclusive"):
                _require_exact_package(root, backup_id)
                return self._mutate_locked(root, operation, backup_id, reason)
        except (BackupFsError, RetentionError, OSError, ValidationError) as exc:
            code = getattr(exc, "error_code", "BACKUP_PIN_INVALID")
            return PinMutationResult(
                status="fail",
                backup_id=(backup_id if BACKUP_ID_PATTERN.fullmatch(backup_id) else None),
                disposition=(
                    "unknown" if code == "BACKUP_PIN_AUDIT_WRITE_FAILED" else "rejected"
                ),
                error_code=code,
            )

    def _mutate_locked(
        self, root: Path, operation: str, backup_id: str, reason: str | None
    ) -> PinMutationResult:
        pin_root = root / ".pins"
        audit_root = root / ".pin-audit"
        ensure_private_directory(pin_root)
        ensure_private_directory(audit_root)
        path = pin_root / f"{backup_id}.json"
        try:
            existing, before_identity = _read_pin(path, backup_id)
        except RetentionError as exc:
            self._audit_rejected(
                audit_root, operation, backup_id, reason, "invalid", exc.error_code
            )
            raise
        if operation == "pin":
            try:
                desired = BackupPinSidecar(
                    backup_id=backup_id,
                    pinned_at_utc=_utc(self.clock()),
                    reason_code=reason,
                )
            except ValidationError as exc:
                self._audit_rejected(
                    audit_root, operation, backup_id, reason, before_identity,
                    "BACKUP_PIN_INVALID",
                )
                raise RetentionError("BACKUP_PIN_INVALID") from exc
            if existing is not None and existing.reason_code != desired.reason_code:
                self._audit_rejected(
                    audit_root, operation, backup_id, reason, before_identity,
                    "BACKUP_PIN_REASON_CONFLICT",
                )
                raise RetentionError("BACKUP_PIN_REASON_CONFLICT")
            disposition = "idempotent" if existing is not None else "created"
            result_identity = before_identity if existing else _digest_model(desired)
        else:
            disposition = "removed" if existing is not None else "idempotent"
            result_identity = "missing"
        operation_id = secrets.token_hex(16)
        journal = PinAuditJournal(
            operation_id=operation_id, operation=operation, backup_id=backup_id,
            requested_reason_code=reason, observed_before_identity=before_identity,
            result_identity=result_identity,
            intended_pin=desired if operation == "pin" else None,
            intended_result=disposition,
            phase="planned", result=None,
            error_code=None, completed_at_utc=None,
        )
        audit_path = audit_root / f"{operation_id}.json"
        _create_journal(audit_path, journal)
        journal = _write_journal(audit_path, journal, phase="mutation_started")
        if operation == "pin" and existing is None:
            payload = _canonical_json(desired.model_dump(mode="json"))
            if not create_bytes_if_absent(path, payload, token=operation_id):
                actual, _ = _read_pin(path, backup_id)
                if actual != desired:
                    raise RetentionError("BACKUP_PIN_INVALID")
        elif operation == "unpin" and existing is not None:
            unlink_durable(path)
        actual, actual_identity = _read_pin(path, backup_id)
        if actual_identity != result_identity:
            raise RetentionError("BACKUP_PIN_INVALID")
        journal = _write_journal(audit_path, journal, phase="mutation_committed")
        _write_journal(
            audit_path, journal, phase="completed", result=disposition,
            completed_at_utc=_utc(self.clock()),
        )
        return PinMutationResult(
            status="pass", backup_id=backup_id, disposition=disposition
        )

    def _audit_rejected(
        self,
        audit_root: Path,
        operation: str,
        backup_id: str,
        reason: str | None,
        before_identity: str,
        error_code: str,
    ) -> None:
        operation_id = secrets.token_hex(16)
        journal = PinAuditJournal(
            operation_id=operation_id, operation=operation, backup_id=backup_id,
            requested_reason_code=reason,
            observed_before_identity=before_identity,
            result_identity=before_identity, intended_pin=None,
            intended_result="rejected", phase="planned", result=None,
            error_code=None, completed_at_utc=None,
        )
        path = audit_root / f"{operation_id}.json"
        _create_journal(path, journal)
        _write_journal(
            path, journal, phase="completed", result="rejected",
            error_code=error_code, completed_at_utc=_utc(self.clock()),
        )


class TempRetentionApplyService:
    """Mutation engine that cannot be assembled without a temp capability."""

    _FILES = ("manifest.json", "database.dump", "watchlist.json")

    def __init__(self, settings: Settings, capability: TempMutationCapability) -> None:
        self.settings = settings
        self.capability = capability
        self.journal_root = (
            settings.BACKUP_DIR.expanduser().parent / "runtime" / "retention-journals"
        )

    def apply(self, plan: RetentionPlan) -> RetentionApplyResult:
        root = self.settings.BACKUP_DIR.expanduser()
        try:
            _require_capability(
                root, self.capability, "BACKUP_RETENTION_APPLY_NOT_AUTHORIZED"
            )
            with backup_root_lock(root, mode="exclusive"):
                self._verify_root_and_packages(root, plan)
                journal_path, journal = self._load_or_create_journal(plan)
                for index, progress in enumerate(journal.candidates):
                    if progress.phase == "deleted":
                        continue
                    progress = self._apply_candidate(
                        root, plan, journal_path, journal, index, progress
                    )
                    journal = _replace_candidate(journal, index, progress)
                deleted = tuple(
                    item.backup_id for item in journal.candidates if item.phase == "deleted"
                )
                cleanup = tuple(
                    item.backup_id
                    for item in journal.candidates
                    if item.phase == "cleanup_required"
                )
                return RetentionApplyResult(
                    status="pass" if not cleanup else "fail",
                    plan_id=plan.plan_id,
                    deleted_backup_ids=deleted,
                    cleanup_required_backup_ids=cleanup,
                    error_code=None if not cleanup else "BACKUP_RETENTION_CLEANUP_REQUIRED",
                )
        except (BackupFsError, RetentionError, OSError, ValidationError) as exc:
            return RetentionApplyResult(
                status="fail", plan_id=plan.plan_id, deleted_backup_ids=(),
                cleanup_required_backup_ids=(),
                error_code=getattr(exc, "error_code", "BACKUP_RETENTION_PLAN_STALE"),
            )

    def _verify_root_and_packages(self, root: Path, plan: RetentionPlan) -> None:
        info = root.lstat()
        if info.st_dev != plan.root_device or info.st_ino != plan.root_inode:
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
        expected = {item.backup_id: item for item in plan.packages}
        visible = {
            path.name
            for path in root.iterdir()
            if BACKUP_ID_PATTERN.fullmatch(path.name)
        }
        journal = self._read_journal_if_present(plan.plan_id)
        _verify_apply_auxiliary_state(root, plan, journal)
        completed = {
            item.backup_id for item in journal.candidates if item.phase != "planned"
        } if journal else set()
        if visible - set(expected) or (set(expected) - visible) - completed:
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
        for backup_id in visible:
            if _package_identity(root, backup_id) != expected[backup_id]:
                raise RetentionError("BACKUP_RETENTION_PLAN_STALE")

    def _load_or_create_journal(
        self, plan: RetentionPlan
    ) -> tuple[Path, RetentionExecutionJournal]:
        ensure_private_directory(self.journal_root)
        path = self.journal_root / f"{plan.plan_id}.json"
        existing = self._read_journal_if_present(plan.plan_id)
        if existing:
            return path, existing
        journal = RetentionExecutionJournal(
            plan_id=plan.plan_id,
            candidates=tuple(
                RetentionCandidateProgress(backup_id=value)
                for value in plan.candidate_backup_ids
            ),
        )
        if not create_bytes_if_absent(
            path, _canonical_json(journal.model_dump(mode="json")), token=plan.plan_id
        ):
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
        return path, journal

    def _read_journal_if_present(
        self, plan_id: str
    ) -> RetentionExecutionJournal | None:
        path = self.journal_root / f"{plan_id}.json"
        if not path.exists() and not path.is_symlink():
            return None
        try:
            value = RetentionExecutionJournal.model_validate_json(
                read_regular_exact(path, max_bytes=MAX_SIDECAR_BYTES)
            )
        except (BackupFsError, ValidationError, OSError) as exc:
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE") from exc
        if value.plan_id != plan_id:
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
        return value

    def _apply_candidate(
        self,
        root: Path,
        plan: RetentionPlan,
        journal_path: Path,
        journal: RetentionExecutionJournal,
        index: int,
        progress: RetentionCandidateProgress,
    ) -> RetentionCandidateProgress:
        pending_root = root / ".retention-pending"
        ensure_private_directory(pending_root)
        final = root / progress.backup_id
        pending = pending_root / f"{progress.backup_id}.{plan.plan_id}"
        root_fd = os.open(
            root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        pending_root_fd = os.open(
            pending_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if os.fstat(root_fd).st_dev != os.fstat(pending_root_fd).st_dev:
                raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
            if progress.phase in {"planned", "rename_started"}:
                if progress.phase == "planned":
                    progress = progress.model_copy(update={"phase": "rename_started"})
                    journal = self._persist_progress(journal_path, journal, index, progress)
                if final.exists():
                    if pending.exists() or pending.is_symlink():
                        raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
                    _rename_no_replace_at(
                        root_fd, progress.backup_id, pending_root_fd, pending.name
                    )
                    os.fsync(root_fd)
                elif not pending.exists():
                    raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
                progress = progress.model_copy(update={"phase": "pending"})
                journal = self._persist_progress(journal_path, journal, index, progress)
            progress = progress.model_copy(update={"phase": "delete_started"})
            journal = self._persist_progress(journal_path, journal, index, progress)
            expected = next(item for item in plan.packages if item.backup_id == progress.backup_id)
            pending_fd = os.open(
                pending.name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=pending_root_fd,
            )
            while progress.file_index < len(self._FILES):
                name = self._FILES[progress.file_index]
                if progress.file_phase == "ready":
                    _verify_pending_progress_fd(
                        pending_fd, expected, progress.file_index
                    )
                    progress = progress.model_copy(update={"file_phase": "unlink_started"})
                    journal = self._persist_progress(journal_path, journal, index, progress)
                try:
                    _verify_expected_file_at(pending_fd, expected, name)
                except FileNotFoundError:
                    if progress.file_phase != "unlink_started":
                        raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
                else:
                    os.unlink(name, dir_fd=pending_fd)
                    os.fsync(pending_fd)
                progress = progress.model_copy(
                    update={"file_index": progress.file_index + 1, "file_phase": "ready"}
                )
                journal = self._persist_progress(journal_path, journal, index, progress)
            if progress.directory_remove_phase == "ready":
                progress = progress.model_copy(
                    update={"directory_remove_phase": "rmdir_started"}
                )
                journal = self._persist_progress(journal_path, journal, index, progress)
            if pending.exists():
                if os.listdir(pending_fd):
                    raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
                os.close(pending_fd)
                pending_fd = -1
                os.rmdir(pending.name, dir_fd=pending_root_fd)
                os.fsync(pending_root_fd)
            progress = progress.model_copy(
                update={"phase": "deleted", "directory_remove_phase": "completed"}
            )
            self._persist_progress(journal_path, journal, index, progress)
            return progress
        except Exception:
            failed = progress.model_copy(update={"phase": "cleanup_required"})
            try:
                self._persist_progress(journal_path, journal, index, failed)
            except BaseException:
                pass
            raise
        finally:
            if "pending_fd" in locals() and pending_fd >= 0:
                os.close(pending_fd)
            os.close(pending_root_fd)
            os.close(root_fd)

    @staticmethod
    def _persist_progress(
        path: Path,
        journal: RetentionExecutionJournal,
        index: int,
        progress: RetentionCandidateProgress,
    ) -> RetentionExecutionJournal:
        updated = _replace_candidate(journal, index, progress)
        atomic_write_bytes(
            path, _canonical_json(updated.model_dump(mode="json")),
            token=secrets.token_hex(8),
        )
        return updated


def _select(
    packages: tuple[RetentionPackageIdentity, ...],
    policy: RetentionPolicy,
    reference: datetime,
    root_info: os.stat_result,
) -> RetentionPlan:
    ordered = sorted(packages, key=lambda item: (item.created_at_utc, item.backup_id), reverse=True)
    verified = [item for item in ordered if item.restore_verified]
    if not verified:
        raise RetentionError("BACKUP_RETENTION_VERIFIED_FLOOR_VIOLATION")
    daily: list[str] = []
    for offset in range(policy.daily_slots):
        day = (reference - timedelta(days=offset)).date()
        match = next((item for item in ordered if item.created_at_utc.date() == day), None)
        if match:
            daily.append(match.backup_id)
    weekly: list[str] = []
    cursor = reference
    for offset in range(policy.weekly_slots):
        week_ref = cursor - timedelta(weeks=offset)
        week = week_ref.isocalendar()[:2]
        match = next(
            (item for item in ordered if item.backup_id not in daily and item.created_at_utc.isocalendar()[:2] == week),
            None,
        )
        if match:
            weekly.append(match.backup_id)
    minimum = [item.backup_id for item in ordered[: policy.minimum_valid_packages]]
    pinned = [item.backup_id for item in ordered if item.pinned]
    held = [item.backup_id for item in ordered if item.recovery_held]
    verified_floor = verified[0].backup_id
    protected = set(daily + weekly + minimum + pinned + held + [verified_floor])
    candidates = [item.backup_id for item in reversed(ordered) if item.backup_id not in protected]
    total = sum(item.package_bytes for item in ordered)
    candidate_bytes = sum(item.package_bytes for item in ordered if item.backup_id in candidates)
    post = total - candidate_bytes
    if total <= policy.archive_budget_bytes:
        budget = "within_budget"
    elif post <= policy.archive_budget_bytes:
        budget = "exceeded_recoverable_after_plan"
    else:
        budget = "exceeded_unrecoverable_after_plan"
    payload = {
        "policy_namespace": POLICY_NAMESPACE,
        "policy_version": POLICY_VERSION,
        "selection_algorithm_version": SELECTION_VERSION,
        "selection_reference_at_utc": reference.isoformat().replace("+00:00", "Z"),
        "root_device": root_info.st_dev,
        "root_inode": root_info.st_ino,
        "packages": [item.model_dump(mode="json") for item in packages],
    }
    return RetentionPlan(
        plan_id=secrets.token_hex(16), created_at_utc=reference,
        selection_reference_at_utc=reference, root_device=root_info.st_dev,
        root_inode=root_info.st_ino,
        canonical_inventory_snapshot_digest=hashlib.sha256(_canonical_json(payload)).hexdigest(),
        packages=packages, daily_winners=tuple(daily), weekly_winners=tuple(weekly),
        minimum_protected=tuple(minimum), pinned_protected=tuple(pinned),
        held_protected=tuple(held), verified_floor_backup_id=verified_floor,
        protected_backup_ids=tuple(sorted(protected)), candidate_backup_ids=tuple(candidates),
        budget_status=budget, total_package_bytes=total, post_plan_package_bytes=post,
    )


def _package_identity(root: Path, backup_id: str) -> RetentionPackageIdentity:
    package = root / backup_id
    manifest_payload = read_regular_exact(package / "manifest.json", max_bytes=MAX_MANIFEST_BYTES)
    manifest = BackupManifest.model_validate_json(manifest_payload)
    dump_hash, dump_size = stream_sha256(package / "database.dump")
    watch_hash, watch_size = stream_sha256(package / "watchlist.json")
    pin_identity, pinned = _optional_file_identity(root / ".pins" / f"{backup_id}.json")
    verification_identity, verified = _optional_file_identity(root / ".verifications" / f"{backup_id}.json")
    holds = sorted((root / ".recovery-holds").glob(f"{backup_id}.*.json")) if (root / ".recovery-holds").exists() else []
    if len(holds) > 1:
        raise RetentionError("BACKUP_RETENTION_INVENTORY_UNSAFE")
    hold_identity, held = _optional_file_identity(holds[0]) if holds else ("missing", False)
    return RetentionPackageIdentity(
        backup_id=backup_id, created_at_utc=manifest.created_at_utc,
        manifest_sha256=hashlib.sha256(manifest_payload).hexdigest(),
        database_dump_sha256=dump_hash, database_dump_size=dump_size,
        watchlist_snapshot_sha256=watch_hash, watchlist_snapshot_size=watch_size,
        package_bytes=sum(path.stat().st_size for path in package.iterdir()),
        verification_identity=verification_identity, pin_identity=pin_identity,
        hold_identity=hold_identity, restore_verified=verified, pinned=pinned,
        recovery_held=held,
    )


def _optional_file_identity(path: Path) -> tuple[str, bool]:
    if not path.exists() and not path.is_symlink():
        return "missing", False
    digest, size = stream_sha256(path)
    return f"sha256:{digest}:size:{size}", True


def _require_exact_package(root: Path, backup_id: str) -> None:
    package = root / backup_id
    info = package.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RetentionError("BACKUP_PIN_INVALID")
    if {path.name for path in package.iterdir()} != {"manifest.json", "database.dump", "watchlist.json"}:
        raise RetentionError("BACKUP_PIN_INVALID")
    _package_identity(root, backup_id)


def _read_pin(path: Path, backup_id: str) -> tuple[BackupPinSidecar | None, str]:
    if not path.exists() and not path.is_symlink():
        return None, "missing"
    try:
        payload = read_regular_exact(path, max_bytes=MAX_SIDECAR_BYTES)
        pin = BackupPinSidecar.model_validate_json(payload)
        if pin.backup_id != backup_id:
            raise ValueError
        return pin, hashlib.sha256(payload).hexdigest()
    except (BackupFsError, ValidationError, ValueError, OSError) as exc:
        raise RetentionError("BACKUP_PIN_INVALID") from exc


def _create_journal(path: Path, journal: PinAuditJournal) -> None:
    if not create_bytes_if_absent(path, _canonical_json(journal.model_dump(mode="json")), token=journal.operation_id):
        raise RetentionError("BACKUP_PIN_AUDIT_WRITE_FAILED")


def _write_journal(path: Path, journal: PinAuditJournal, **changes: object) -> PinAuditJournal:
    updated = journal.model_copy(update=changes)
    try:
        atomic_write_bytes(path, _canonical_json(updated.model_dump(mode="json")), token=secrets.token_hex(8))
    except BackupFsError as exc:
        raise RetentionError("BACKUP_PIN_AUDIT_WRITE_FAILED") from exc
    return updated


def _require_capability(root: Path, capability: TempMutationCapability, code: str) -> None:
    info = root.lstat()
    if (
        capability._secret is not _CAPABILITY_SECRET
        or root.resolve() != capability.root
        or info.st_dev != capability.device
        or info.st_ino != capability.inode
    ):
        raise RetentionError(code)


def _has_entries(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    validate_backup_root(path)
    return any(path.iterdir())


def _has_unfinished_pin_audit(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    validate_backup_root(path)
    for item in path.iterdir():
        try:
            journal = PinAuditJournal.model_validate_json(
                read_regular_exact(item, max_bytes=MAX_SIDECAR_BYTES)
            )
        except (BackupFsError, ValidationError, OSError):
            return True
        if journal.phase != "completed":
            return True
    return False


def _digest_model(model: ContractModel) -> str:
    return hashlib.sha256(_canonical_json(model.model_dump(mode="json"))).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RetentionError("BACKUP_RETENTION_INVENTORY_UNSAFE")
    return value.astimezone(UTC)


async def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"pin", "unpin", "pin-reconcile", "apply", "resume"}:
        from app.deploy.backup_retention_authorization import (
            ProductionMutationCommandAdapter,
        )

        print(ProductionMutationCommandAdapter.reject().model_dump_json())
        return 1
    if args != ["plan"]:
        print("usage: python -m app.deploy.backup_retention plan")
        return 2
    result = await RetentionPlanService(load_settings()).plan()
    plan = result.plan
    report = RetentionPlanReport(
        status=result.status,
        plan_id=plan.plan_id if plan else None,
        selection_reference_at_utc=(
            plan.selection_reference_at_utc if plan else None
        ),
        protected_backup_ids=plan.protected_backup_ids if plan else (),
        candidate_backup_ids=plan.candidate_backup_ids if plan else (),
        budget_status=plan.budget_status if plan else None,
        total_package_bytes=plan.total_package_bytes if plan else None,
        post_plan_package_bytes=plan.post_plan_package_bytes if plan else None,
        error_code=result.error_code,
    )
    print(report.model_dump_json())
    return 0 if report.status == "pass" else 1


def _replace_candidate(
    journal: RetentionExecutionJournal,
    index: int,
    progress: RetentionCandidateProgress,
) -> RetentionExecutionJournal:
    values = list(journal.candidates)
    values[index] = progress
    return journal.model_copy(update={"candidates": tuple(values)})


def _rename_no_replace_at(
    source_dir_fd: int,
    source_name: str,
    target_dir_fd: int,
    target_name: str,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source_name)
    target_bytes = os.fsencode(target_name)
    if sys.platform == "darwin" and hasattr(libc, "renameatx_np"):
        result = libc.renameatx_np(
            source_dir_fd, source_bytes, target_dir_fd, target_bytes, 0x00000004
        )
    elif hasattr(libc, "renameat2"):
        result = libc.renameat2(
            source_dir_fd, source_bytes, target_dir_fd, target_bytes, 1
        )
    else:
        raise RetentionError("BACKUP_RETENTION_NOREPLACE_UNSUPPORTED")
    if result != 0:
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
        raise RetentionError("BACKUP_RETENTION_NOREPLACE_UNSUPPORTED")


def _verify_pending_progress_fd(
    pending_fd: int, expected: RetentionPackageIdentity, file_index: int
) -> None:
    names = ("manifest.json", "database.dump", "watchlist.json")
    observed = set(os.listdir(pending_fd))
    if any(name in observed for name in names[:file_index]):
        raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
    if observed != set(names[file_index:]):
        raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
    for name in names[file_index:]:
        _verify_expected_file_at(pending_fd, expected, name)


def _verify_expected_file_at(
    pending_fd: int, expected: RetentionPackageIdentity, name: str
) -> None:
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=pending_fd,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
        digest = hashlib.sha256()
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
            digest.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(fd)
        if (
            info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size
        ) != (
            after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode), after.st_size
        ):
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
        observed_digest = digest.hexdigest()
        size = info.st_size
    finally:
        os.close(fd)
    values = {
        "manifest.json": (expected.manifest_sha256, None),
        "database.dump": (
            expected.database_dump_sha256,
            expected.database_dump_size,
        ),
        "watchlist.json": (
            expected.watchlist_snapshot_sha256,
            expected.watchlist_snapshot_size,
        ),
    }
    expected_digest, expected_size = values[name]
    if observed_digest != expected_digest or (
        expected_size is not None and size != expected_size
    ):
        raise RetentionError("BACKUP_RETENTION_PLAN_STALE")


def _verify_apply_auxiliary_state(
    root: Path,
    plan: RetentionPlan,
    journal: RetentionExecutionJournal | None,
) -> None:
    reserved = {
        ".backup.lock", ".tmp", ".pins", ".pin-audit", ".verifications",
        ".recovery-holds", ".retention-pending",
    }
    if any(
        not BACKUP_ID_PATTERN.fullmatch(path.name) and path.name not in reserved
        for path in root.iterdir()
    ):
        raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
    expected_ids = {item.backup_id for item in plan.packages}
    for directory in (root / ".pins", root / ".verifications"):
        if not directory.exists() and not directory.is_symlink():
            continue
        validate_backup_root(directory)
        for path in directory.iterdir():
            if not path.name.endswith(".json") or path.name[:-5] not in expected_ids:
                raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
    holds = root / ".recovery-holds"
    if holds.exists() or holds.is_symlink():
        validate_backup_root(holds)
        for path in holds.iterdir():
            if not any(path.name.startswith(f"{backup_id}.") for backup_id in expected_ids):
                raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
    if _has_unfinished_pin_audit(root / ".pin-audit"):
        raise RetentionError("BACKUP_RETENTION_PLAN_STALE")
    pending_root = root / ".retention-pending"
    if pending_root.exists() or pending_root.is_symlink():
        validate_backup_root(pending_root)
        allowed = set()
        if journal:
            allowed = {
                f"{item.backup_id}.{plan.plan_id}"
                for item in journal.candidates
                if item.phase not in {"planned", "deleted"}
            }
        if {path.name for path in pending_root.iterdir()} - allowed:
            raise RetentionError("BACKUP_RETENTION_PLAN_STALE")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
