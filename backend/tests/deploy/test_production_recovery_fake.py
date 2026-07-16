from __future__ import annotations

import asyncio
import threading

import pytest

from app.deploy.production_recovery_fake import (
    CrashInjection,
    FakeProductionRecoveryOrchestrator,
    FakeCleanupCoordinator,
    FakeRecoveryCrash,
    FakeRecoveryWorld,
)
from app.deploy.production_recovery_record import (
    ProductionRecoveryError,
    TempRecoveryRecordStore,
    create_temp_recovery_capability,
)
from tests.deploy.test_production_recovery_models import make_record


def setup_rehearsal(tmp_path, *, watchlist=False, crash=None):
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(make_record())
    world = FakeRecoveryWorld()
    orchestrator = FakeProductionRecoveryOrchestrator(
        store, world, watchlist_switch=watchlist, crash=crash
    )
    return store, record, world, orchestrator


def test_r0_r7_full_flow_without_watchlist_switch(tmp_path) -> None:
    store, record, world, orchestrator = setup_rehearsal(tmp_path)
    result = orchestrator.run(record)
    assert result.status == "pass"
    assert result.phase == "completed"
    assert result.report_desensitized == "yes"
    assert result.services_stopped
    assert world.env_state == "staged"
    assert world.watchlist_state == "original"
    assert world.fixture_root is not None
    assert b"replacement" in (world.fixture_root / "active.env").read_bytes()
    assert b"original" in (world.fixture_root / "watchlist.json").read_bytes()
    assert b"original" in (world.fixture_root / "protected.env").read_bytes()
    persisted = store.read(record.incident_id)
    assert persisted.monitor_first_write_observed == "no"
    assert persisted.watchlist_was_switched == "no"


def test_protection_backup_can_only_use_explicit_skip_branch(tmp_path) -> None:
    store, record, _world, _orchestrator = setup_rehearsal(tmp_path)
    orchestrator = FakeProductionRecoveryOrchestrator(
        store, FakeRecoveryWorld(), protection_required=False
    )
    result = orchestrator.run(record)
    persisted = store.read(record.incident_id)
    assert result.status == "pass"
    assert persisted.protection_backup_status == "skipped_authorized"
    assert persisted.authorizations.protection_skip_authorized == "yes"


def test_r0_r7_full_flow_with_watchlist_and_write_observation(tmp_path) -> None:
    store, record, world, orchestrator = setup_rehearsal(
        tmp_path, watchlist=True
    )
    world.write_observed = True
    result = orchestrator.run(record)
    assert result.phase == "completed"
    assert world.watchlist_state == "staged"
    persisted = store.read(record.incident_id)
    assert persisted.watchlist_was_switched == "yes"
    assert persisted.monitor_first_write_observed == "yes"


@pytest.mark.parametrize(
    "phase",
    [
        "protection_backup_started",
        "replacement_create_started",
        "identity_commit_started",
        "restore_started",
        "verification_started",
        "services_stop_started",
        "config_protection_started",
        "env_switch_started",
        "watchlist_switch_started",
        "application_start_started",
        "readiness_started",
        "monitor_start_started",
    ],
)
@pytest.mark.parametrize("moment", ["after_intent", "after_action"])
def test_each_forward_intent_resumes_after_crash(tmp_path, phase, moment) -> None:
    crash = CrashInjection(phase=phase, moment=moment)
    store, record, _world, orchestrator = setup_rehearsal(
        tmp_path, watchlist=True, crash=crash
    )
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.run(record)
    resumed_world = FakeRecoveryWorld()
    resumed_owner = FakeProductionRecoveryOrchestrator(
        store, resumed_world, watchlist_switch=True
    )
    resumed = resumed_owner.run(store.read(record.incident_id))
    assert resumed.status == "pass"
    assert resumed.phase == "completed"
    operation = {
        "protection_backup_started": "protection_backup",
        "replacement_create_started": "create_replacement",
        "identity_commit_started": "commit_identity",
        "restore_started": "restore",
        "verification_started": "verify",
        "services_stop_started": "stop_services",
        "config_protection_started": "protect_config",
        "env_switch_started": "switch_env",
        "watchlist_switch_started": "switch_watchlist",
        "application_start_started": "start_replacement_app",
        "readiness_started": "check_replacement",
        "monitor_start_started": "start_monitor",
    }[phase]
    assert resumed_world.calls[operation] == 1


def test_rollback_restores_original_without_watchlist_phase(tmp_path) -> None:
    crash = CrashInjection(phase="readiness_started", moment="after_action")
    store, record, world, orchestrator = setup_rehearsal(tmp_path, crash=crash)
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.run(record)
    result = orchestrator.rollback(store.read(record.incident_id))
    assert result.phase == "rolled_back"
    assert world.env_state == "original"
    assert world.watchlist_state == "original"
    assert world.application_database == "original"
    assert not world.monitor_running
    assert world.fixture_root is not None
    assert b"original" in (world.fixture_root / "active.env").read_bytes()
    assert b"original" in (world.fixture_root / "protected.env").read_bytes()


def test_rollback_restores_switched_watchlist(tmp_path) -> None:
    crash = CrashInjection(phase="readiness_started", moment="after_action")
    store, record, world, orchestrator = setup_rehearsal(
        tmp_path, watchlist=True, crash=crash
    )
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.run(record)
    result = orchestrator.rollback(store.read(record.incident_id))
    assert result.phase == "rolled_back"
    assert world.watchlist_state == "original"


@pytest.mark.parametrize(
    "phase",
    [
        "rollback_started",
        "rollback_application_stop_started",
        "rollback_env_started",
        "rollback_watchlist_started",
        "rollback_application_start_started",
        "rollback_readiness_started",
    ],
)
@pytest.mark.parametrize("moment", ["after_intent", "after_action"])
def test_each_rollback_intent_resumes_after_crash(tmp_path, phase, moment) -> None:
    if phase == "rollback_started" and moment == "after_action":
        pytest.skip("Monitor is already stopped before rollback, so no action runs")
    forward_crash = CrashInjection(
        phase="readiness_started", moment="after_action"
    )
    store, record, _world, orchestrator = setup_rehearsal(
        tmp_path, watchlist=True, crash=forward_crash
    )
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.run(record)
    orchestrator.crash = CrashInjection(phase=phase, moment=moment)
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.rollback(store.read(record.incident_id))
    resumed_world = FakeRecoveryWorld()
    resumed_owner = FakeProductionRecoveryOrchestrator(
        store, resumed_world, watchlist_switch=True
    )
    result = resumed_owner.rollback(store.read(record.incident_id))
    assert result.status == "pass"
    assert result.phase == "rolled_back"


def test_monitor_start_intent_is_manual_reconciliation_fence(tmp_path) -> None:
    crash = CrashInjection(phase="monitor_start_started", moment="after_intent")
    store, record, _world, orchestrator = setup_rehearsal(tmp_path, crash=crash)
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.run(record)
    result = orchestrator.rollback(store.read(record.incident_id))
    assert result.status == "partial"
    assert result.rollback_status == "manual_required"
    assert result.phase == "monitor_start_started"


def test_unknown_config_state_stops_with_manual_reconciliation(tmp_path) -> None:
    crash = CrashInjection(phase="env_switch_started", moment="after_action")
    store, record, world, orchestrator = setup_rehearsal(tmp_path, crash=crash)
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.run(record)
    assert world.fixture_root is not None
    (world.fixture_root / "active.env").write_bytes(b"third-state\n")
    resumed_world = FakeRecoveryWorld()
    resumed_owner = FakeProductionRecoveryOrchestrator(store, resumed_world)
    result = resumed_owner.run(store.read(record.incident_id))
    assert result.status == "fail"
    assert result.error_code == "PRODUCTION_RECOVERY_MANUAL_RECONCILIATION_REQUIRED"
    assert store.read(record.incident_id).manual_reconciliation_required == "yes"


def test_replaced_protection_copy_stops_resume(tmp_path) -> None:
    crash = CrashInjection(
        phase="config_protection_started", moment="after_action"
    )
    store, record, world, owner = setup_rehearsal(tmp_path, crash=crash)
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    assert world.fixture_root is not None
    (world.fixture_root / "protected.env").write_bytes(b"replaced\n")
    resumed = FakeProductionRecoveryOrchestrator(store, FakeRecoveryWorld()).run(
        store.read(record.incident_id)
    )
    assert resumed.status == "fail"
    assert resumed.rollback_status == "manual_required"


def test_env_file_commit_before_snapshot_is_reconciled_as_completed(tmp_path) -> None:
    store, record, world, owner = setup_rehearsal(tmp_path)

    def crash_after_file() -> None:
        raise FakeRecoveryCrash("switch_env:after_file")

    world.operation_hooks["switch_env_after_file"] = crash_after_file
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    assert world.fixture_root is not None
    before = (world.fixture_root / "active.env").stat()
    resumed_world = FakeRecoveryWorld()
    resumed = FakeProductionRecoveryOrchestrator(store, resumed_world).run(
        store.read(record.incident_id)
    )
    assert resumed.phase == "completed"
    after = (world.fixture_root / "active.env").stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def test_partial_protection_copy_resumes_without_overwriting_first_copy(
    tmp_path,
) -> None:
    store, record, world, owner = setup_rehearsal(tmp_path)

    def crash_after_first_copy() -> None:
        raise FakeRecoveryCrash("protect_config:after_env_copy")

    world.operation_hooks["protect_config_after_env_copy"] = (
        crash_after_first_copy
    )
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    assert world.fixture_root is not None
    first = (world.fixture_root / "protected.env").stat()
    resumed = FakeProductionRecoveryOrchestrator(store, FakeRecoveryWorld()).run(
        store.read(record.incident_id)
    )
    second = (world.fixture_root / "protected.env").stat()
    assert resumed.phase == "completed"
    assert (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def test_valid_staged_env_before_durable_intent_is_rejected(tmp_path) -> None:
    crash = CrashInjection(
        phase="config_protection_started", moment="after_action"
    )
    store, record, world, owner = setup_rehearsal(tmp_path, crash=crash)
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    assert world.fixture_root is not None
    (world.fixture_root / "active.env").write_bytes(
        b"DATABASE_URL=fake://replacement\n"
    )
    result = FakeProductionRecoveryOrchestrator(store, FakeRecoveryWorld()).run(
        store.read(record.incident_id)
    )
    assert result.status == "fail"
    assert result.rollback_status == "manual_required"


@pytest.mark.parametrize("moment", ["after_facts", "after_intent"])
def test_skip_authorization_windows_resume_with_new_owner(tmp_path, moment) -> None:
    crash = CrashInjection(
        phase="protection_backup_skipped_authorized", moment=moment
    )
    store, record, _world, _orchestrator = setup_rehearsal(tmp_path)
    owner = FakeProductionRecoveryOrchestrator(
        store,
        FakeRecoveryWorld(),
        protection_required=False,
        crash=crash,
    )
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    resumed = FakeProductionRecoveryOrchestrator(
        store, FakeRecoveryWorld(), protection_required=False
    ).run(store.read(record.incident_id))
    assert resumed.phase == "completed"


@pytest.mark.parametrize("moment", ["after_facts", "after_intent"])
def test_monitor_authorization_windows_resume_with_new_owner(tmp_path, moment) -> None:
    crash = CrashInjection(phase="monitor_start_authorized", moment=moment)
    store, record, _world, owner = setup_rehearsal(tmp_path, crash=crash)
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    resumed = FakeProductionRecoveryOrchestrator(store, FakeRecoveryWorld()).run(
        store.read(record.incident_id)
    )
    assert resumed.phase == "completed"


def test_readiness_failure_remains_rollback_eligible(tmp_path) -> None:
    store, record, world, orchestrator = setup_rehearsal(tmp_path)
    world.failures.add("check_replacement")
    failed = orchestrator.run(record)
    assert failed.error_code == "PRODUCTION_RECOVERY_READINESS_FAILED"
    assert failed.rollback_status == "not_started"
    world.failures.clear()
    rolled_back = orchestrator.rollback(store.read(record.incident_id))
    assert rolled_back.phase == "rolled_back"


async def test_full_flow_cancellation_settles_current_step_and_stops_next_gate(
    tmp_path,
) -> None:
    store, record, world, orchestrator = setup_rehearsal(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def block_create() -> None:
        started.set()
        release.wait(timeout=2)

    world.operation_hooks["create_replacement"] = block_create
    task = asyncio.create_task(orchestrator.run_async(record))
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    persisted = store.read(record.incident_id)
    assert persisted.phase == "replacement_created"
    assert not world.replacement_identified
    resumed_world = FakeRecoveryWorld()
    resumed_owner = FakeProductionRecoveryOrchestrator(store, resumed_world)
    result = resumed_owner.run(persisted)
    assert result.phase == "completed"


async def test_rollback_cancellation_settles_stop_before_env_restore(tmp_path) -> None:
    forward_crash = CrashInjection(
        phase="readiness_started", moment="after_action"
    )
    store, record, world, orchestrator = setup_rehearsal(
        tmp_path, crash=forward_crash
    )
    with pytest.raises(FakeRecoveryCrash):
        orchestrator.run(record)
    orchestrator.crash = None
    started = threading.Event()
    release = threading.Event()

    def block_stop() -> None:
        started.set()
        release.wait(timeout=2)

    world.operation_hooks["rollback_stop_app"] = block_stop
    task = asyncio.create_task(
        orchestrator.rollback_async(store.read(record.incident_id))
    )
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    persisted = store.read(record.incident_id)
    assert persisted.phase == "rollback_application_stopped"
    assert world.env_state == "staged"


async def test_child_cleanup_completes_after_rollback(tmp_path) -> None:
    forward_crash = CrashInjection(
        phase="readiness_started", moment="after_action"
    )
    store, record, world, owner = setup_rehearsal(tmp_path, crash=forward_crash)
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    rolled_back = owner.rollback(store.read(record.incident_id))
    main = await FakeCleanupCoordinator(store, world).execute(
        store.read(record.incident_id)
    )
    assert rolled_back.phase == "rolled_back"
    assert main.cleanup_completed == "yes"
    assert not world.replacement_created
    assert world.application_database == "original"
    assert world.calls["cleanup_drop"] == 1


async def test_cleanup_after_drop_crash_resumes_without_second_drop(tmp_path) -> None:
    forward_crash = CrashInjection(
        phase="readiness_started", moment="after_action"
    )
    store, record, world, owner = setup_rehearsal(tmp_path, crash=forward_crash)
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    owner.rollback(store.read(record.incident_id))

    def crash_after_drop() -> None:
        raise FakeRecoveryCrash("cleanup:after_drop")

    world.operation_hooks["cleanup_after_drop"] = crash_after_drop
    with pytest.raises(FakeRecoveryCrash):
        await FakeCleanupCoordinator(store, world).execute(
            store.read(record.incident_id)
        )
    resumed_world = FakeRecoveryWorld()
    resumed_world.attach(store.capability.root / "fake-recovery-world")
    main = await FakeCleanupCoordinator(store, resumed_world).execute(
        store.read(record.incident_id)
    )
    assert main.cleanup_completed == "yes"
    assert resumed_world.calls["cleanup_drop"] == 1


async def test_cleanup_is_forbidden_while_replacement_is_active(tmp_path) -> None:
    store, record, world, owner = setup_rehearsal(tmp_path)
    owner.run(record)
    with pytest.raises(ProductionRecoveryError, match="CLEANUP_FORBIDDEN"):
        await FakeCleanupCoordinator(store, world).execute(
            store.read(record.incident_id)
        )


def test_main_requested_child_absent_recreates_exact_child(tmp_path) -> None:
    forward_crash = CrashInjection(
        phase="readiness_started", moment="after_action"
    )
    store, record, world, owner = setup_rehearsal(tmp_path, crash=forward_crash)
    with pytest.raises(FakeRecoveryCrash):
        owner.run(record)
    owner.rollback(store.read(record.incident_id))
    coordinator = FakeCleanupCoordinator(store, world)
    main, child = coordinator.request(store.read(record.incident_id))
    (store.root / f"cleanup-{child.cleanup_record_id}.json").unlink()
    resumed_main, recreated = coordinator.request(main)
    assert resumed_main == main
    assert recreated.phase == "planned"
    assert recreated.cleanup_record_id == child.cleanup_record_id
