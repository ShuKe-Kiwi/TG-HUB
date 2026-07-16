from __future__ import annotations

import asyncio
import pickle
import threading
from pathlib import Path

import pytest

from app.deploy.production_recovery_orchestrator import (
    FakeRecoveryAction,
    TempRecoveryOrchestrator,
)
from app.deploy.production_recovery_models import (
    ProductionCleanupRecord,
    ProductionRecoveryRecord,
)
from app.deploy.production_recovery_record import (
    ProductionRecoveryAdapter,
    ProductionRecoveryError,
    TempRecoveryRecordStore,
    create_temp_recovery_capability,
)
from tests.deploy.test_production_recovery_models import make_record


def test_temp_record_round_trip_and_private_modes(tmp_path) -> None:
    root = tmp_path / "runtime"
    capability = create_temp_recovery_capability(root)
    store = TempRecoveryRecordStore(capability)
    record = make_record()
    record = store.create(record)
    assert store.read(record.incident_id) == record
    assert root.stat().st_mode & 0o777 == 0o700
    assert (store.root / f"{record.incident_id}.json").stat().st_mode & 0o777 == 0o600


def test_capability_is_non_serializable_and_production_fails_closed(tmp_path) -> None:
    capability = create_temp_recovery_capability(tmp_path / "runtime")
    with pytest.raises(pickle.PicklingError):
        pickle.dumps(capability)
    with pytest.raises(ProductionRecoveryError, match="NOT_AUTHORIZED"):
        ProductionRecoveryAdapter(capability)


def test_capability_rejects_non_temp_root_before_creating_it() -> None:
    root = Path(__file__).resolve().parents[3] / ".forbidden-recovery-root"
    assert not root.exists()
    with pytest.raises(ProductionRecoveryError, match="NOT_AUTHORIZED"):
        create_temp_recovery_capability(root)
    assert not root.exists()


def test_store_revalidates_capability_before_creating_children(tmp_path) -> None:
    root = tmp_path / "runtime"
    capability = create_temp_recovery_capability(root)
    root.rmdir()
    root.mkdir()
    with pytest.raises(ProductionRecoveryError, match="NOT_AUTHORIZED"):
        TempRecoveryRecordStore(capability)
    assert not (root / "production-recovery").exists()


def test_replaced_lock_inode_fails_closed(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(make_record())
    store.lock_path.unlink()
    store.lock_path.write_bytes(b"")
    with pytest.raises(ProductionRecoveryError, match="LOCK_INVALID"):
        store.read(record.incident_id)


def test_record_is_create_once_and_stale_advance_is_rejected(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    initial = make_record()
    record = store.create(initial)
    with pytest.raises(ProductionRecoveryError, match="ALREADY_EXISTS"):
        store.create(initial)
    current = store.advance(record, "protection_backup_started")
    assert current.phase == "protection_backup_started"
    with pytest.raises(ProductionRecoveryError, match="RECORD_STALE"):
        store.advance(record, "protection_backup_started")


async def test_record_failure_prevents_external_action(tmp_path, monkeypatch) -> None:
    store = TempRecoveryRecordStore(create_temp_recovery_capability(tmp_path / "runtime"))
    action = FakeRecoveryAction()
    record = store.create(make_record())
    monkeypatch.setattr(
        "app.deploy.production_recovery_record.atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_WRITE_FAILED")
        ),
    )
    with pytest.raises(ProductionRecoveryError, match="RECORD_WRITE_FAILED"):
        await TempRecoveryOrchestrator(store).execute_after_intent(
            record, "protection_backup_started", action
        )
    assert action.calls == 0


async def test_intent_is_durable_before_fake_action(tmp_path) -> None:
    store = TempRecoveryRecordStore(create_temp_recovery_capability(tmp_path / "runtime"))
    observed: list[str] = []
    record = make_record()
    record = store.create(record)
    action = FakeRecoveryAction(result="ok")

    def inspect() -> str:
        persisted = ProductionRecoveryRecord.model_validate_json(
            (store.root / f"{record.incident_id}.json").read_bytes()
        )
        observed.append(persisted.phase)
        action()
        return "ok"
    durable, result = await TempRecoveryOrchestrator(store).execute_after_intent(
        record, "protection_backup_started", inspect
    )
    assert (durable.phase, result, observed, action.calls) == (
        "protection_backup_started",
        "ok",
        ["protection_backup_started"],
        1,
    )


def test_facts_use_compare_and_swap_and_activation_is_monotonic(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(make_record(phase="env_switch_started"))
    updated = store.update_facts(record, replacement_activated="yes")
    assert updated.replacement_activated == "yes"
    with pytest.raises(ProductionRecoveryError, match="RECORD_STALE"):
        store.update_facts(record, retryable="yes")
    with pytest.raises(ProductionRecoveryError, match="FACT_UPDATE_INVALID"):
        store.update_facts(updated, replacement_activated="no")


def test_frozen_resource_identity_cannot_be_rewritten(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(make_record())
    rewritten = record.resources.model_copy(
        update={"replacement_database_identity": "other"}
    )
    with pytest.raises(ProductionRecoveryError, match="FACT_UPDATE_INVALID"):
        store.update_facts(record, resources=rewritten)
    filled = record.resources.model_copy(update={"protected_env_sha256": "6" * 64})
    updated = store.update_facts(record, resources=filled)
    assert updated.resources.protected_env_sha256 == "6" * 64


def test_lock_identity_survives_new_store_and_rejects_replacement(tmp_path) -> None:
    capability = create_temp_recovery_capability(tmp_path / "runtime")
    first = TempRecoveryRecordStore(capability)
    record = first.create(make_record())
    second = TempRecoveryRecordStore(capability)
    assert second.read(record.incident_id) == record
    second.lock_path.unlink()
    second.lock_path.write_bytes(b"")
    with pytest.raises(ProductionRecoveryError, match="LOCK_INVALID"):
        TempRecoveryRecordStore(capability).read(record.incident_id)


def test_child_cleanup_is_create_once_and_bound_to_main(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    main = store.create(make_record())
    main = store.update_facts(
        main, cleanup_requested="yes", cleanup_record_id="c" * 32
    )
    assert main.stable_lock_identity is not None
    child = ProductionCleanupRecord(
        cleanup_record_id="c" * 32,
        incident_id=main.incident_id,
        replacement_database_identity=main.resources.replacement_database_identity,
        replacement_identity_token=main.resources.replacement_identity_token,
        expected_owner_identity=main.resources.expected_owner_identity,
        stable_lock_identity=main.stable_lock_identity,
    )
    store.create_cleanup(main, child)
    store.create_cleanup(main, child)
    assert store.read_cleanup(main) == child
    started = store.advance_cleanup(main, child, "cleanup_started")
    with pytest.raises(ProductionRecoveryError, match="DROP_NOT_OBSERVED"):
        store.advance_cleanup(main, started, "cleanup_completed")
    observed = store.update_cleanup_facts(main, started, drop_observed="yes")
    completed = store.advance_cleanup(main, observed, "cleanup_completed")
    assert completed.phase == "cleanup_completed"
    main = store.update_facts(main, cleanup_completed="yes")
    assert main.cleanup_completed == "yes"


def test_child_cannot_claim_drop_before_cleanup_started(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    main = store.create(make_record())
    main = store.update_facts(
        main, cleanup_requested="yes", cleanup_record_id="d" * 32
    )
    assert main.stable_lock_identity is not None
    child = ProductionCleanupRecord(
        cleanup_record_id="d" * 32,
        incident_id=main.incident_id,
        replacement_database_identity=main.resources.replacement_database_identity,
        replacement_identity_token=main.resources.replacement_identity_token,
        expected_owner_identity=main.resources.expected_owner_identity,
        stable_lock_identity=main.stable_lock_identity,
    )
    store.create_cleanup(main, child)
    with pytest.raises(ProductionRecoveryError, match="CLEANUP_FACT_UPDATE_INVALID"):
        store.update_cleanup_facts(main, child, drop_observed="yes")
    with pytest.raises(ProductionRecoveryError, match="CLEANUP_NOT_COMPLETED"):
        store.update_facts(main, cleanup_completed="yes")


def test_fill_once_and_historical_facts_cannot_be_rewritten(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(
        make_record(
            phase="monitor_started",
            monitor_generation_id="generation-1",
            monitor_write_baseline="baseline-1",
        )
    )
    record = store.update_facts(
        record,
        protection_backup_id="20260716T130000.000000Z-" + "e" * 32,
        monitor_first_write_observed="no",
        manual_reconciliation_required="yes",
    )
    for changes in (
        {"protection_backup_id": "20260716T140000.000000Z-" + "f" * 32},
        {"monitor_generation_id": "generation-2"},
        {"monitor_first_write_observed": "yes"},
        {"manual_reconciliation_required": "no"},
    ):
        with pytest.raises(ProductionRecoveryError, match="FACT_UPDATE_INVALID"):
            store.update_facts(record, **changes)


def test_monitor_baseline_cannot_be_reserved_before_readiness(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(make_record())
    with pytest.raises(ProductionRecoveryError, match="FACT_UPDATE_INVALID"):
        store.update_facts(
            record,
            monitor_generation_id="generation-early",
            monitor_write_baseline="baseline-early",
        )


def test_verification_result_is_phase_aware_and_terminal(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    wrong_phase = store.create(make_record())
    with pytest.raises(ProductionRecoveryError, match="FACT_UPDATE_INVALID"):
        store.update_facts(wrong_phase, verification_result="passed")

    other_store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "other-runtime")
    )
    verifying = other_store.create(
        make_record(incident_id="a" * 32, phase="verification_started")
    )
    passed = other_store.update_facts(verifying, verification_result="passed")
    with pytest.raises(ProductionRecoveryError, match="FACT_UPDATE_INVALID"):
        other_store.update_facts(passed, verification_result="failed")


async def test_cancellation_waits_for_worker_then_propagates(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(make_record())
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def action() -> str:
        started.set()
        await release.wait()
        completed.set()
        return "done"

    task = asyncio.create_task(
        TempRecoveryOrchestrator(store).execute_after_intent(
            record, "protection_backup_started", action
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()


async def test_cancellation_waits_for_blocking_thread_then_propagates(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = store.create(make_record())
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def action() -> str:
        started.set()
        release.wait(timeout=2)
        completed.set()
        return "done"

    task = asyncio.create_task(
        TempRecoveryOrchestrator(store).execute_after_intent(
            record, "protection_backup_started", action
        )
    )
    while not started.is_set():
        await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    with pytest.raises(ProductionRecoveryError, match="IN_PROGRESS"):
        with store.operation_lease(record):
            pass
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert completed.is_set()
