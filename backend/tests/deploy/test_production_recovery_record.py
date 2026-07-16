from __future__ import annotations

import pickle
from pathlib import Path

import pytest

from app.deploy.production_recovery_orchestrator import (
    FakeRecoveryAction,
    TempRecoveryOrchestrator,
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
    store.create(record)
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
    store.lock_path.unlink()
    store.lock_path.write_bytes(b"")
    with pytest.raises(ProductionRecoveryError, match="LOCK_INVALID"):
        store.create(make_record())


def test_record_is_create_once_and_stale_advance_is_rejected(tmp_path) -> None:
    store = TempRecoveryRecordStore(
        create_temp_recovery_capability(tmp_path / "runtime")
    )
    record = make_record()
    store.create(record)
    with pytest.raises(ProductionRecoveryError, match="ALREADY_EXISTS"):
        store.create(record)
    current = store.advance(record, "protection_backup_started")
    assert current.phase == "protection_backup_started"
    with pytest.raises(ProductionRecoveryError, match="RECORD_STALE"):
        store.advance(record, "protection_backup_started")


def test_record_failure_prevents_external_action(tmp_path, monkeypatch) -> None:
    store = TempRecoveryRecordStore(create_temp_recovery_capability(tmp_path / "runtime"))
    action = FakeRecoveryAction()
    store.create(make_record())
    monkeypatch.setattr(
        "app.deploy.production_recovery_record.atomic_write_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ProductionRecoveryError("PRODUCTION_RECOVERY_RECORD_WRITE_FAILED")
        ),
    )
    with pytest.raises(ProductionRecoveryError, match="RECORD_WRITE_FAILED"):
        TempRecoveryOrchestrator(store).execute_after_intent(
            make_record(), "protection_backup_started", action
        )
    assert action.calls == 0


def test_intent_is_durable_before_fake_action(tmp_path) -> None:
    store = TempRecoveryRecordStore(create_temp_recovery_capability(tmp_path / "runtime"))
    observed: list[str] = []
    record = make_record()
    store.create(record)
    action = FakeRecoveryAction(result="ok")

    def inspect() -> str:
        observed.append(store.read(record.incident_id).phase)
        action()
        return "ok"
    durable, result = TempRecoveryOrchestrator(store).execute_after_intent(
        record, "protection_backup_started", inspect
    )
    assert (durable.phase, result, observed, action.calls) == (
        "protection_backup_started",
        "ok",
        ["protection_backup_started"],
        1,
    )
