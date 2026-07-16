"""Temp-only R0-R7 and rollback rehearsal for P6-Deploy-4D-4B."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field as PydanticField

from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    ensure_private_directory,
    read_regular_exact,
)
from app.deploy.production_recovery_models import (
    ProductionCleanupRecord,
    ProductionRecoveryRecord,
    RecoveryPhase,
)
from app.deploy.production_recovery_record import (
    ProductionRecoveryError,
    TempRecoveryRecordStore,
)

UTC = timezone.utc


class FakeRecoveryCrash(RuntimeError):
    pass


class FakeRecoveryActionError(RuntimeError):
    pass


class FakeRecoveryCancellationRequested(RuntimeError):
    pass


@dataclass
class CrashInjection:
    phase: RecoveryPhase
    moment: Literal["after_facts", "after_intent", "after_action"]
    consumed: bool = False

    def raise_if_due(self, phase: RecoveryPhase, moment: str) -> None:
        if not self.consumed and self.phase == phase and self.moment == moment:
            self.consumed = True
            raise FakeRecoveryCrash(f"{phase}:{moment}")


class FakeWorldSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    protection_created: bool = False
    replacement_created: bool = False
    replacement_identified: bool = False
    restored: bool = False
    verified: bool = False
    monitor_running: bool = True
    session_lease_free: bool = False
    application_database: Literal["original", "replacement", "stopped", "unknown"] = "original"
    connections_drained: bool = False
    env_state: Literal["original", "staged", "unknown"] = "original"
    watchlist_state: Literal["original", "staged", "unknown"] = "original"
    application_ready: bool = False
    monitor_generation: str | None = None
    write_observed: bool = False
    config_protected: bool = False
    calls: dict[str, int] = PydanticField(default_factory=dict)


@dataclass
class FakeRecoveryWorld:
    protection_created: bool = False
    replacement_created: bool = False
    replacement_identified: bool = False
    restored: bool = False
    verified: bool = False
    monitor_running: bool = True
    session_lease_free: bool = False
    application_database: Literal["original", "replacement", "stopped", "unknown"] = "original"
    connections_drained: bool = False
    env_state: Literal["original", "staged", "unknown"] = "original"
    watchlist_state: Literal["original", "staged", "unknown"] = "original"
    application_ready: bool = False
    monitor_generation: str | None = None
    write_observed: bool = False
    config_protected: bool = False
    failures: set[str] = field(default_factory=set)
    operation_hooks: dict[str, Callable[[], None]] = field(default_factory=dict)
    calls: dict[str, int] = field(default_factory=dict)
    fixture_root: Path | None = None

    def attach(self, root: Path) -> None:
        if self.fixture_root is not None:
            return
        self.fixture_root = root
        ensure_private_directory(root)
        snapshot_path = root / "world-state.json"
        if snapshot_path.exists() or snapshot_path.is_symlink():
            self._load_snapshot()
            self._reconcile_config_files()
            self._persist()
            return
        self._write_fixture("active.env", b"DATABASE_URL=fake://original\n")
        self._write_fixture("watchlist.json", b'{"generation":"original"}\n')
        self._persist()

    def perform(self, operation: str) -> None:
        self.calls[operation] = self.calls.get(operation, 0) + 1
        if operation in self.failures:
            raise FakeRecoveryActionError(operation)
        if operation in self.operation_hooks:
            self.operation_hooks[operation]()
        handlers = {
            "protection_backup": self._protection,
            "create_replacement": self._create,
            "commit_identity": self._identity,
            "restore": self._restore,
            "verify": self._verify,
            "stop_services": self._stop_services,
            "protect_config": self._protect_config,
            "switch_env": self._switch_env,
            "switch_watchlist": self._switch_watchlist,
            "start_replacement_app": self._start_replacement,
            "check_replacement": self._check_replacement,
            "start_monitor": self._start_monitor,
            "observe_monitor": lambda: None,
            "rollback_stop_monitor": self._stop_monitor,
            "rollback_stop_app": self._stop_app,
            "rollback_env": self._rollback_env,
            "rollback_watchlist": self._rollback_watchlist,
            "start_original_app": self._start_original,
            "check_original": self._check_original,
        }
        handlers[operation]()
        self._persist()

    def _protection(self) -> None:
        self.protection_created = True

    def _create(self) -> None:
        self.replacement_created = True

    def _identity(self) -> None:
        if not self.replacement_created:
            raise FakeRecoveryActionError("replacement_missing")
        self.replacement_identified = True

    def _restore(self) -> None:
        if not self.replacement_identified:
            raise FakeRecoveryActionError("identity_missing")
        self.restored = True

    def _verify(self) -> None:
        if not self.restored:
            raise FakeRecoveryActionError("restore_missing")
        self.verified = True

    def _stop_monitor(self) -> None:
        self.monitor_running = False
        self.session_lease_free = True

    def _stop_app(self) -> None:
        self.application_database = "stopped"
        self.connections_drained = True
        self.application_ready = False

    def _stop_services(self) -> None:
        self._stop_monitor()
        self._stop_app()

    def _protect_config(self) -> None:
        if self.fixture_root is None:
            raise FakeRecoveryActionError("fixture_not_attached")
        env = read_regular_exact(self.fixture_root / "active.env", max_bytes=4096)
        watchlist = read_regular_exact(
            self.fixture_root / "watchlist.json", max_bytes=4096
        )
        if env != b"DATABASE_URL=fake://original\n" or watchlist != (
            b'{"generation":"original"}\n'
        ):
            raise FakeRecoveryActionError("config_identity_unknown")
        status = self._protection_file_status()
        if status == "unknown":
            raise FakeRecoveryActionError("config_identity_unknown")
        protected_env = self.fixture_root / "protected.env"
        protected_watchlist = self.fixture_root / "protected-watchlist.json"
        if not protected_env.exists():
            self._write_fixture("protected.env", env)
            hook = self.operation_hooks.get("protect_config_after_env_copy")
            if hook is not None:
                hook()
        if not protected_watchlist.exists():
            self._write_fixture("protected-watchlist.json", watchlist)
        self.config_protected = True

    def _switch_env(self) -> None:
        if self.env_state == "unknown":
            raise FakeRecoveryActionError("config_identity_unknown")
        self.env_state = "staged"
        self._write_fixture("active.env", b"DATABASE_URL=fake://replacement\n")
        hook = self.operation_hooks.get("switch_env_after_file")
        if hook is not None:
            hook()

    def _switch_watchlist(self) -> None:
        if self.watchlist_state == "unknown":
            raise FakeRecoveryActionError("watchlist_identity_unknown")
        self.watchlist_state = "staged"
        self._write_fixture("watchlist.json", b'{"generation":"staged"}\n')
        hook = self.operation_hooks.get("switch_watchlist_after_file")
        if hook is not None:
            hook()

    def _start_replacement(self) -> None:
        if self.env_state != "staged":
            raise FakeRecoveryActionError("replacement_not_active")
        self.application_database = "replacement"
        self.connections_drained = False

    def _check_replacement(self) -> None:
        if self.application_database != "replacement":
            raise FakeRecoveryActionError("replacement_not_running")
        self.application_ready = True

    def _start_monitor(self) -> None:
        if not self.application_ready:
            raise FakeRecoveryActionError("application_not_ready")
        self.monitor_running = True
        self.session_lease_free = False
        self.monitor_generation = "fake-generation-1"

    def _rollback_env(self) -> None:
        if self.env_state == "unknown":
            raise FakeRecoveryActionError("config_identity_unknown")
        if self.fixture_root is None:
            raise FakeRecoveryActionError("fixture_not_attached")
        protected = read_regular_exact(
            self.fixture_root / "protected.env", max_bytes=4096
        )
        if protected != b"DATABASE_URL=fake://original\n":
            raise FakeRecoveryActionError("config_identity_unknown")
        self.env_state = "original"
        self._write_fixture("active.env", protected)

    def _rollback_watchlist(self) -> None:
        if self.watchlist_state == "unknown":
            raise FakeRecoveryActionError("watchlist_identity_unknown")
        if self.fixture_root is None:
            raise FakeRecoveryActionError("fixture_not_attached")
        protected = read_regular_exact(
            self.fixture_root / "protected-watchlist.json", max_bytes=4096
        )
        if protected != b'{"generation":"original"}\n':
            raise FakeRecoveryActionError("watchlist_identity_unknown")
        self.watchlist_state = "original"
        self._write_fixture("watchlist.json", protected)

    def _start_original(self) -> None:
        if self.env_state != "original":
            raise FakeRecoveryActionError("original_not_active")
        self.application_database = "original"
        self.connections_drained = False

    def _check_original(self) -> None:
        if self.application_database != "original":
            raise FakeRecoveryActionError("original_not_running")
        self.application_ready = True

    def _write_fixture(self, name: str, payload: bytes) -> None:
        if self.fixture_root is None:
            raise FakeRecoveryActionError("fixture_not_attached")
        atomic_write_bytes(
            self.fixture_root / name,
            payload,
            token=f"fake-{self.calls.get(name, 0)}",
        )
        self.calls[name] = self.calls.get(name, 0) + 1

    def operation_status(
        self, operation: str
    ) -> Literal["completed", "pending", "unknown"]:
        if operation == "protection_backup":
            complete = self.protection_created
        elif operation == "create_replacement":
            complete = self.replacement_created
        elif operation == "commit_identity":
            complete = self.replacement_identified
        elif operation == "restore":
            complete = self.restored
        elif operation == "verify":
            complete = self.verified
        elif operation in {"stop_services", "rollback_stop_app"}:
            complete = (
                self.application_database == "stopped" and self.connections_drained
            )
        elif operation == "rollback_stop_monitor":
            complete = not self.monitor_running and self.session_lease_free
        elif operation == "protect_config":
            status = self._protection_file_status()
            if status == "unknown":
                return "unknown"
            complete = status == "completed"
            if complete and not self.config_protected:
                self.config_protected = True
                self._persist()
        elif operation == "switch_env":
            if self.env_state == "unknown":
                return "unknown"
            complete = self.env_state == "staged"
        elif operation == "switch_watchlist":
            if self.watchlist_state == "unknown":
                return "unknown"
            complete = self.watchlist_state == "staged"
        elif operation == "start_replacement_app":
            if self.application_database == "unknown":
                return "unknown"
            complete = self.application_database == "replacement"
        elif operation == "check_replacement":
            complete = self.application_ready
        elif operation == "start_monitor":
            if self.monitor_running and self.monitor_generation not in {
                None,
                "fake-generation-1",
            }:
                return "unknown"
            complete = (
                self.monitor_running
                and self.monitor_generation == "fake-generation-1"
            )
        elif operation == "rollback_env":
            if self.env_state == "unknown":
                return "unknown"
            complete = self.env_state == "original"
        elif operation == "rollback_watchlist":
            if self.watchlist_state == "unknown":
                return "unknown"
            complete = self.watchlist_state == "original"
        elif operation == "start_original_app":
            if self.application_database == "unknown":
                return "unknown"
            complete = self.application_database == "original"
        elif operation == "check_original":
            complete = (
                self.application_database == "original" and self.application_ready
            )
        elif operation == "observe_monitor":
            complete = False
        else:
            return "unknown"
        return "completed" if complete else "pending"

    def _protection_file_status(
        self,
    ) -> Literal["completed", "pending", "unknown"]:
        if self.fixture_root is None:
            return "unknown"
        expected = {
            "protected.env": b"DATABASE_URL=fake://original\n",
            "protected-watchlist.json": b'{"generation":"original"}\n',
        }
        present = 0
        for name, payload in expected.items():
            path = self.fixture_root / name
            if not path.exists() and not path.is_symlink():
                continue
            try:
                observed = read_regular_exact(path, max_bytes=4096)
            except (BackupFsError, OSError):
                return "unknown"
            if observed != payload:
                return "unknown"
            present += 1
        return "completed" if present == len(expected) else "pending"

    def _snapshot(self) -> FakeWorldSnapshot:
        return FakeWorldSnapshot(
            protection_created=self.protection_created,
            replacement_created=self.replacement_created,
            replacement_identified=self.replacement_identified,
            restored=self.restored,
            verified=self.verified,
            monitor_running=self.monitor_running,
            session_lease_free=self.session_lease_free,
            application_database=self.application_database,
            connections_drained=self.connections_drained,
            env_state=self.env_state,
            watchlist_state=self.watchlist_state,
            application_ready=self.application_ready,
            monitor_generation=self.monitor_generation,
            write_observed=self.write_observed,
            config_protected=self.config_protected,
            calls=dict(self.calls),
        )

    def _persist(self) -> None:
        if self.fixture_root is None:
            raise FakeRecoveryActionError("fixture_not_attached")
        payload = self._snapshot().model_dump_json().encode() + b"\n"
        atomic_write_bytes(
            self.fixture_root / "world-state.json",
            payload,
            token=f"snapshot-{sum(self.calls.values())}",
        )

    def _load_snapshot(self) -> None:
        if self.fixture_root is None:
            raise FakeRecoveryActionError("fixture_not_attached")
        snapshot = FakeWorldSnapshot.model_validate_json(
            read_regular_exact(
                self.fixture_root / "world-state.json", max_bytes=128 * 1024
            )
        )
        for name in FakeWorldSnapshot.model_fields:
            if name != "schema_version":
                setattr(self, name, getattr(snapshot, name))

    def _reconcile_config_files(self) -> None:
        if self.fixture_root is None:
            raise FakeRecoveryActionError("fixture_not_attached")
        env = read_regular_exact(self.fixture_root / "active.env", max_bytes=4096)
        watchlist = read_regular_exact(
            self.fixture_root / "watchlist.json", max_bytes=4096
        )
        env_states = {
            b"DATABASE_URL=fake://original\n": "original",
            b"DATABASE_URL=fake://replacement\n": "staged",
        }
        watchlist_states = {
            b'{"generation":"original"}\n': "original",
            b'{"generation":"staged"}\n': "staged",
        }
        observed_env = env_states.get(env, "unknown")
        observed_watchlist = watchlist_states.get(watchlist, "unknown")
        self.env_state = observed_env
        self.watchlist_state = observed_watchlist
        protection_status = self._protection_file_status()
        if protection_status == "completed":
            self.config_protected = True
        elif protection_status == "unknown":
            self.config_protected = False


class FakeRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["pass", "fail", "partial"]
    incident_id: str
    phase: RecoveryPhase
    replacement_created: bool
    restore_completed: bool
    verification_completed: bool
    services_stopped: bool
    config_switched: bool
    application_ready: bool
    monitor_started: bool
    rollback_status: Literal["not_started", "completed", "manual_required"]
    cleanup_required: bool
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"


class FakeGuardedDatabaseCleanupPrimitive:
    def __init__(self, world: FakeRecoveryWorld) -> None:
        self.world = world

    async def drop_once(
        self, record: ProductionCleanupRecord
    ) -> Literal["dropped", "absent"]:
        del record
        if self.world.env_state == "staged":
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_CLEANUP_FORBIDDEN")
        self.world.calls["cleanup_drop"] = (
            self.world.calls.get("cleanup_drop", 0) + 1
        )
        if not self.world.replacement_created:
            self.world._persist()
            return "absent"
        self.world.replacement_created = False
        self.world.replacement_identified = False
        self.world.restored = False
        self.world.verified = False
        self.world._persist()
        return "dropped"


class FakeCleanupCoordinator:
    def __init__(
        self,
        store: TempRecoveryRecordStore,
        world: FakeRecoveryWorld,
        primitive: FakeGuardedDatabaseCleanupPrimitive | None = None,
    ) -> None:
        self.store = store
        self.world = world
        self.primitive = primitive or FakeGuardedDatabaseCleanupPrimitive(world)

    def request(
        self, main: ProductionRecoveryRecord
    ) -> tuple[ProductionRecoveryRecord, ProductionCleanupRecord]:
        main = self.store.read(main.incident_id)
        if self.world.env_state == "staged" or main.replacement_activated == "yes" and (
            main.phase != "rolled_back"
        ):
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_CLEANUP_FORBIDDEN")
        eligible = main.phase == "rolled_back" or (
            main.phase in {"restore_started", "verification_started"}
            and main.replacement_activated == "no"
        )
        if not eligible:
            raise ProductionRecoveryError("PRODUCTION_RECOVERY_CLEANUP_FORBIDDEN")
        cleanup_id = hashlib.sha256(
            f"{main.incident_id}:replacement-cleanup".encode()
        ).hexdigest()[:32]
        newly_requested = main.cleanup_requested == "no"
        if newly_requested:
            main = self.store.update_facts(
                main, cleanup_requested="yes", cleanup_record_id=cleanup_id
            )
        if main.cleanup_record_id != cleanup_id or main.stable_lock_identity is None:
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_IDENTITY_INVALID"
            )
        expected_child = ProductionCleanupRecord(
            cleanup_record_id=cleanup_id,
            incident_id=main.incident_id,
            replacement_database_identity=(
                main.resources.replacement_database_identity
            ),
            replacement_identity_token=main.resources.replacement_identity_token,
            expected_owner_identity=main.resources.expected_owner_identity,
            stable_lock_identity=main.stable_lock_identity,
        )
        if newly_requested:
            self.store.create_cleanup(main, expected_child)
        else:
            try:
                return main, self.store.read_cleanup(main)
            except ProductionRecoveryError as exc:
                if exc.error_code != "PRODUCTION_RECOVERY_RECORD_INVALID":
                    raise
                child = expected_child
            self.store.create_cleanup(main, child)
        return main, self.store.read_cleanup(main)

    async def execute(self, main: ProductionRecoveryRecord) -> ProductionRecoveryRecord:
        main, child = self.request(main)
        if child.phase == "planned":
            child = self.store.advance_cleanup(main, child, "cleanup_started")
        if child.phase == "cleanup_started" and child.drop_observed == "no":
            if self.world.replacement_created:
                await self.primitive.drop_once(child)
                hook = self.world.operation_hooks.get("cleanup_after_drop")
                if hook is not None:
                    hook()
            child = self.store.update_cleanup_facts(
                main, child, drop_observed="yes"
            )
        if child.phase == "cleanup_started":
            child = self.store.advance_cleanup(main, child, "cleanup_completed")
        if child.phase != "cleanup_completed" or child.drop_observed != "yes":
            raise ProductionRecoveryError(
                "PRODUCTION_RECOVERY_CLEANUP_NOT_COMPLETED"
            )
        return self.store.update_facts(main, cleanup_completed="yes")


class FakeProductionRecoveryOrchestrator:
    def __init__(
        self,
        store: TempRecoveryRecordStore,
        world: FakeRecoveryWorld,
        *,
        watchlist_switch: bool = False,
        protection_required: bool = True,
        crash: CrashInjection | None = None,
    ) -> None:
        self.store = store
        self.world = world
        self.world.attach(store.capability.root / "fake-recovery-world")
        self.watchlist_switch = watchlist_switch
        self.protection_required = protection_required
        self.crash = crash
        self._cancel_requested = threading.Event()

    async def run_async(
        self, record: ProductionRecoveryRecord
    ) -> FakeRecoveryResult:
        worker = asyncio.create_task(asyncio.to_thread(self.run, record))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            self._cancel_requested.set()
            try:
                await worker
            except FakeRecoveryCancellationRequested:
                pass
            finally:
                raise

    async def rollback_async(
        self, record: ProductionRecoveryRecord
    ) -> FakeRecoveryResult:
        worker = asyncio.create_task(asyncio.to_thread(self.rollback, record))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            self._cancel_requested.set()
            try:
                await worker
            except FakeRecoveryCancellationRequested:
                pass
            finally:
                raise

    def run(self, record: ProductionRecoveryRecord) -> FakeRecoveryResult:
        try:
            current = self._run_forward(record)
            return self._result(current, "pass")
        except FakeRecoveryCrash:
            raise
        except FakeRecoveryActionError as exc:
            current = self.store.read(record.incident_id)
            error_code = self._action_error_code(str(exc))
            changes: dict[str, object] = {
                "last_error_code": error_code,
                "retryable": "yes",
            }
            if error_code == "PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED":
                changes["manual_reconciliation_required"] = "yes"
                changes["retryable"] = "no"
            if current.phase == "verification_started":
                changes.update(
                    verification_result="failed",
                    verification_error_code="PRODUCTION_RECOVERY_VERIFICATION_FAILED",
                )
            current = self.store.update_facts(current, **changes)
            return self._result(current, "fail", error_code)
        except ProductionRecoveryError as exc:
            current = self.store.read(record.incident_id)
            if exc.error_code == "PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED":
                current = self.store.update_facts(
                    current,
                    manual_reconciliation_required="yes",
                    last_error_code=exc.error_code,
                    retryable="no",
                )
            return self._result(current, "fail", exc.error_code)

    def rollback(self, record: ProductionRecoveryRecord) -> FakeRecoveryResult:
        current = self.store.read(record.incident_id)
        if current.phase in {
            "monitor_start_started", "monitor_started", "monitor_write_observed",
            "completed",
        }:
            current = self.store.update_facts(
                current,
                manual_reconciliation_required="yes",
                last_error_code="PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED",
            )
            return self._result(current, "partial")
        try:
            if current.phase != "rollback_started" and not current.phase.startswith("rollback_"):
                with self.store.operation_lease(current) as lease:
                    current = lease.advance("rollback_started")
                self._crash("rollback_started", "after_intent")
            current = self._run_rollback(current)
            return self._result(current, "pass")
        except FakeRecoveryActionError as exc:
            current = self.store.read(record.incident_id)
            error_code = self._action_error_code(str(exc))
            if error_code == "PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED":
                current = self.store.update_facts(
                    current,
                    manual_reconciliation_required="yes",
                    last_error_code=error_code,
                    retryable="no",
                )
                return self._result(current, "partial", error_code)
            return self._result(
                current, "fail", "PRODUCTION_RECOVERY_ROLLBACK_FAILED"
            )
        except ProductionRecoveryError as exc:
            current = self.store.read(record.incident_id)
            return self._result(current, "fail", exc.error_code)

    def _run_forward(self, record: ProductionRecoveryRecord) -> ProductionRecoveryRecord:
        current = self.store.read(record.incident_id)
        while current.phase != "completed":
            if self._cancel_requested.is_set():
                raise FakeRecoveryCancellationRequested
            phase = current.phase
            self._validate_forward_external_facts(current)
            if phase == "planned":
                if self.protection_required:
                    current = self._step(
                        current, "protection_backup_started", "protection_backup",
                        "protection_backup_completed", self._protection_facts,
                    )
                else:
                    with self.store.operation_lease(current) as lease:
                        auth = lease.current.authorizations.model_copy(
                            update={"protection_skip_authorized": "yes"}
                        )
                        current = lease.update_facts(
                            authorizations=auth,
                            protection_backup_status="skipped_authorized",
                        )
                        self._crash(
                            "protection_backup_skipped_authorized", "after_facts"
                        )
                        current = lease.advance("protection_backup_skipped_authorized")
                        self._crash(
                            "protection_backup_skipped_authorized", "after_intent"
                        )
                continue
            if phase == "protection_backup_started":
                current = self._resume_step(
                    current,
                    "protection_backup",
                    "protection_backup_completed",
                    self._protection_facts,
                )
            elif phase in {"protection_backup_completed", "protection_backup_skipped_authorized"}:
                current = self._step(
                    current,
                    "replacement_create_started",
                    "create_replacement",
                    "replacement_created",
                )
            elif phase == "replacement_create_started":
                current = self._resume_step(current, "create_replacement", "replacement_created")
            elif phase == "replacement_created":
                current = self._step(
                    current,
                    "identity_commit_started",
                    "commit_identity",
                    "identity_committed",
                )
            elif phase == "identity_commit_started":
                current = self._resume_step(current, "commit_identity", "identity_committed")
            elif phase == "identity_committed":
                current = self._step(current, "restore_started", "restore", "restore_completed")
            elif phase == "restore_started":
                current = self._resume_step(current, "restore", "restore_completed")
            elif phase == "restore_completed":
                current = self._step(
                    current,
                    "verification_started",
                    "verify",
                    "verification_completed",
                    lambda: {"verification_result": "passed"},
                )
            elif phase == "verification_started":
                current = self._resume_step(
                    current,
                    "verify",
                    "verification_completed",
                    lambda: {"verification_result": "passed"},
                )
            elif phase == "verification_completed":
                current = self._step(
                    current,
                    "services_stop_started",
                    "stop_services",
                    "services_stopped",
                    self._stop_facts,
                )
            elif phase == "services_stop_started":
                current = self._resume_step(
                    current, "stop_services", "services_stopped", self._stop_facts
                )
            elif phase == "services_stopped":
                current = self._step(
                    current,
                    "config_protection_started",
                    "protect_config",
                    "config_protection_completed",
                    self._config_facts,
                )
            elif phase == "config_protection_started":
                current = self._resume_step(
                    current,
                    "protect_config",
                    "config_protection_completed",
                    self._config_facts,
                )
            elif phase == "config_protection_completed":
                current = self._step(
                    current,
                    "env_switch_started",
                    "switch_env",
                    "env_switched",
                    lambda: {"replacement_activated": "yes"},
                )
            elif phase == "env_switch_started":
                current = self._resume_step(
                    current,
                    "switch_env",
                    "env_switched",
                    lambda: {"replacement_activated": "yes"},
                )
            elif phase == "env_switched":
                current = self._after_env(current)
            elif phase == "watchlist_switch_started":
                current = self._resume_step(
                    current,
                    "switch_watchlist",
                    "watchlist_switched",
                    lambda: {"watchlist_was_switched": "yes"},
                )
            elif phase == "watchlist_switched":
                current = self._advance(current, "config_switched")
            elif phase == "config_switched":
                current = self._step(
                    current,
                    "application_start_started",
                    "start_replacement_app",
                    "application_started",
                )
            elif phase == "application_start_started":
                current = self._resume_step(current, "start_replacement_app", "application_started")
            elif phase == "application_started":
                current = self._step(
                    current,
                    "readiness_started",
                    "check_replacement",
                    "readiness_passed",
                )
            elif phase == "readiness_started":
                current = self._resume_step(current, "check_replacement", "readiness_passed")
            elif phase == "readiness_passed":
                current = self._authorize_monitor(current)
            elif phase == "monitor_start_authorized":
                current = self._step(
                    current,
                    "monitor_start_started",
                    "start_monitor",
                    "monitor_started",
                )
            elif phase == "monitor_start_started":
                current = self._resume_step(current, "start_monitor", "monitor_started")
            elif phase == "monitor_started":
                current = self._observe(current)
            elif phase == "monitor_write_observed":
                current = self._advance(current, "completed")
            else:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_PHASE_INVALID")
        return current

    def _validate_forward_external_facts(
        self, record: ProductionRecoveryRecord
    ) -> None:
        before_env_intent = {
            "planned",
            "protection_backup_started",
            "protection_backup_completed",
            "protection_backup_skipped_authorized",
            "replacement_create_started",
            "replacement_created",
            "identity_commit_started",
            "identity_committed",
            "restore_started",
            "restore_completed",
            "verification_started",
            "verification_completed",
            "services_stop_started",
            "services_stopped",
            "config_protection_started",
            "config_protection_completed",
        }
        after_env_result = {
            "env_switched",
            "watchlist_switch_started",
            "watchlist_switched",
            "config_switched",
            "application_start_started",
            "application_started",
            "readiness_started",
            "readiness_passed",
            "monitor_start_authorized",
            "monitor_start_started",
            "monitor_started",
            "monitor_write_observed",
        }
        if record.phase in before_env_intent and self.world.env_state != "original":
            raise FakeRecoveryActionError("external_fact_unknown")
        if record.phase in after_env_result and self.world.env_state != "staged":
            raise FakeRecoveryActionError("external_fact_unknown")
        if record.phase in after_env_result:
            expected_watchlist = (
                "staged" if record.watchlist_was_switched == "yes" else "original"
            )
            if record.phase == "watchlist_switch_started":
                expected_watchlist = self.world.watchlist_state
            if self.world.watchlist_state != expected_watchlist:
                raise FakeRecoveryActionError("external_fact_unknown")

    def _run_rollback(self, record: ProductionRecoveryRecord) -> ProductionRecoveryRecord:
        current = self.store.read(record.incident_id)
        while current.phase != "rolled_back":
            if self._cancel_requested.is_set():
                raise FakeRecoveryCancellationRequested
            phase = current.phase
            if phase == "rollback_started":
                current = self._resume_step(
                    current,
                    "rollback_stop_monitor",
                    "rollback_monitor_stopped",
                    lambda: {"monitor_stopped": "yes", "session_lease_free": "yes"},
                )
            elif phase == "rollback_monitor_stopped":
                current = self._step(
                    current,
                    "rollback_application_stop_started",
                    "rollback_stop_app",
                    "rollback_application_stopped",
                    lambda: {
                        "application_stopped": "yes",
                        "application_connections_drained": "yes",
                    },
                )
            elif phase == "rollback_application_stop_started":
                current = self._resume_step(
                    current,
                    "rollback_stop_app",
                    "rollback_application_stopped",
                    lambda: {
                        "application_stopped": "yes",
                        "application_connections_drained": "yes",
                    },
                )
            elif phase == "rollback_application_stopped":
                current = self._step(
                    current,
                    "rollback_env_started",
                    "rollback_env",
                    "rollback_env_completed",
                )
            elif phase == "rollback_env_started":
                current = self._resume_step(current, "rollback_env", "rollback_env_completed")
            elif phase == "rollback_env_completed":
                if current.watchlist_was_switched == "yes":
                    current = self._step(
                        current,
                        "rollback_watchlist_started",
                        "rollback_watchlist",
                        "rollback_watchlist_completed",
                    )
                else:
                    current = self._step(
                        current,
                        "rollback_application_start_started",
                        "start_original_app",
                        "rollback_application_started",
                    )
            elif phase == "rollback_watchlist_started":
                current = self._resume_step(
                    current, "rollback_watchlist", "rollback_watchlist_completed"
                )
            elif phase == "rollback_watchlist_completed":
                current = self._step(
                    current,
                    "rollback_application_start_started",
                    "start_original_app",
                    "rollback_application_started",
                )
            elif phase == "rollback_application_start_started":
                current = self._resume_step(
                    current, "start_original_app", "rollback_application_started"
                )
            elif phase == "rollback_application_started":
                current = self._step(
                    current,
                    "rollback_readiness_started",
                    "check_original",
                    "rollback_readiness_passed",
                )
            elif phase == "rollback_readiness_started":
                current = self._resume_step(current, "check_original", "rollback_readiness_passed")
            elif phase == "rollback_readiness_passed":
                current = self._advance(current, "rolled_back")
            else:
                raise ProductionRecoveryError("PRODUCTION_RECOVERY_PHASE_INVALID")
        return current

    def _step(
        self,
        record: ProductionRecoveryRecord,
        intent: RecoveryPhase,
        operation: str,
        result: RecoveryPhase,
        facts=None,
    ) -> ProductionRecoveryRecord:
        with self.store.operation_lease(record) as lease:
            lease.advance(intent)
            self._crash(intent, "after_intent")
            self.world.perform(operation)
            self._crash(intent, "after_action")
            if operation == "protect_config":
                resources = lease.current.resources.model_copy(
                    update={
                        "protected_env_sha256": lease.current.resources.original_env_sha256,
                        "protected_watchlist_sha256": (
                            lease.current.resources.original_watchlist_sha256
                        ),
                    }
                )
                lease.update_facts(resources=resources)
            if facts:
                payload = facts()
                if payload:
                    lease.update_facts(**payload)
            return lease.advance(result)

    def _resume_step(
        self,
        record: ProductionRecoveryRecord,
        operation: str,
        result: RecoveryPhase,
        facts=None,
    ) -> ProductionRecoveryRecord:
        with self.store.operation_lease(record) as lease:
            status = self.world.operation_status(operation)
            if status == "unknown":
                raise FakeRecoveryActionError("external_fact_unknown")
            if status == "pending":
                self.world.perform(operation)
                self._crash(record.phase, "after_action")
            if operation == "protect_config":
                resources = lease.current.resources.model_copy(
                    update={
                        "protected_env_sha256": lease.current.resources.original_env_sha256,
                        "protected_watchlist_sha256": (
                            lease.current.resources.original_watchlist_sha256
                        ),
                    }
                )
                lease.update_facts(resources=resources)
            if facts:
                payload = facts()
                if payload:
                    lease.update_facts(**payload)
            return lease.advance(result)

    def _advance(
        self, record: ProductionRecoveryRecord, target: RecoveryPhase
    ) -> ProductionRecoveryRecord:
        with self.store.operation_lease(record) as lease:
            return lease.advance(target)

    def _after_env(self, record: ProductionRecoveryRecord) -> ProductionRecoveryRecord:
        if self.watchlist_switch:
            with self.store.operation_lease(record) as lease:
                auth = lease.current.authorizations.model_copy(
                    update={"watchlist_switch_authorized": "yes"}
                )
                current = lease.update_facts(
                    authorizations=auth, watchlist_switch_authorized="yes"
                )
            return self._step(
                current,
                "watchlist_switch_started",
                "switch_watchlist",
                "watchlist_switched",
                lambda: {"watchlist_was_switched": "yes"},
            )
        return self._advance(record, "config_switched")

    def _authorize_monitor(self, record: ProductionRecoveryRecord) -> ProductionRecoveryRecord:
        with self.store.operation_lease(record) as lease:
            auth = lease.current.authorizations.model_copy(
                update={"monitor_start_authorized": "yes"}
            )
            lease.update_facts(
                authorizations=auth,
                monitor_generation_id="fake-generation-1",
                monitor_write_baseline="fake-baseline-0",
            )
            self._crash("monitor_start_authorized", "after_facts")
            current = lease.advance("monitor_start_authorized")
            self._crash("monitor_start_authorized", "after_intent")
            return current

    def _observe(self, record: ProductionRecoveryRecord) -> ProductionRecoveryRecord:
        with self.store.operation_lease(record) as lease:
            self.world.perform("observe_monitor")
            observed = "yes" if self.world.write_observed else "no"
            lease.update_facts(
                monitor_first_write_observed=observed,
                monitor_first_write_observed_at_utc=datetime.now(UTC),
            )
            if observed == "yes":
                lease.advance("monitor_write_observed")
            return lease.advance("completed")

    def _crash(self, phase: RecoveryPhase, moment: str) -> None:
        if self.crash:
            self.crash.raise_if_due(phase, moment)

    @staticmethod
    def _protection_facts() -> dict[str, object]:
        return {
            "protection_backup_id": "20260716T150000.000000Z-" + "9" * 32,
            "protection_manifest_sha256": "6" * 64,
            "protection_database_dump_sha256": "7" * 64,
            "protection_watchlist_sha256": "8" * 64,
            "protection_backup_status": "completed",
        }

    def _stop_facts(self) -> dict[str, object]:
        return {
            "monitor_stopped": "yes",
            "session_lease_free": "yes",
            "application_stopped": "yes",
            "application_connections_drained": "yes",
        }

    @staticmethod
    def _config_facts() -> dict[str, object]:
        return {}

    def _result(
        self,
        record: ProductionRecoveryRecord,
        status: Literal["pass", "fail", "partial"],
        error_code: str | None = None,
    ) -> FakeRecoveryResult:
        if record.phase == "rolled_back":
            rollback_status = "completed"
        elif record.manual_reconciliation_required == "yes":
            rollback_status = "manual_required"
        else:
            rollback_status = "not_started"
        return FakeRecoveryResult(
            status=status,
            incident_id=record.incident_id,
            phase=record.phase,
            replacement_created=self.world.replacement_created,
            restore_completed=self.world.restored,
            verification_completed=record.verification_result == "passed",
            services_stopped=(
                record.application_stopped == "yes"
                and record.application_connections_drained == "yes"
            ),
            config_switched=record.replacement_activated == "yes",
            application_ready=self.world.application_ready,
            monitor_started=(
                self.world.monitor_running
                and record.phase
                in {"monitor_started", "monitor_write_observed", "completed"}
            ),
            rollback_status=rollback_status,
            cleanup_required=(
                record.cleanup_requested == "yes"
                and record.cleanup_completed == "no"
            ),
            error_code=error_code,
        )

    @staticmethod
    def _action_error_code(operation: str) -> str:
        if operation in {
            "config_identity_unknown",
            "watchlist_identity_unknown",
            "external_fact_unknown",
        }:
            return "PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED"
        return {
            "verify": "PRODUCTION_RECOVERY_VERIFICATION_FAILED",
            "check_replacement": "PRODUCTION_RECOVERY_READINESS_FAILED",
            "stop_services": "PRODUCTION_RECOVERY_SERVICE_STOP_FAILED",
            "restore": "PRODUCTION_RECOVERY_RESTORE_FAILED",
        }.get(operation, "PRODUCTION_RECOVERY_TARGET_INVALID")
