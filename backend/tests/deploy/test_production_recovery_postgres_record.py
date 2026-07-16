from __future__ import annotations

import json

import pytest

from app.deploy.backup_models import PgConnectionSpec
from app.deploy.production_recovery_postgres import (
    FakeObservationProvider,
    FakeTempPostgresAdapter,
    TempPostgresCapabilityIssuer,
    TempPostgresRehearsalError,
)
from app.deploy.production_recovery_postgres_record import (
    TempPostgresRehearsalRecordStore,
    open_rehearsal_store_for_resume,
)


def spec() -> PgConnectionSpec:
    return PgConnectionSpec(
        host="127.0.0.1",
        port=5432,
        user="fixture",
        database="fixture_maintenance",
    )


def create_store(tmp_path):
    provider = FakeObservationProvider()
    capability = TempPostgresCapabilityIssuer().issue_initial(
        root=tmp_path / "runtime", spec=spec(), provider=provider
    )
    return TempPostgresRehearsalRecordStore(capability), provider


def advance_to_workflow_terminal(store, record):
    for phase in (
        "source_create_started",
        "source_created",
        "source_identity_commit_started",
        "source_identity_committed",
        "dump_started",
        "dump_committed",
        "replacement_bound",
        "workflow_terminal",
    ):
        record = store.advance(record, phase)
    return record


def test_record_is_private_durable_and_contains_cleanup_identity(tmp_path) -> None:
    store, _ = create_store(tmp_path)
    record = store.create()
    assert store.read() == record
    assert store.root.stat().st_mode & 0o777 == 0o700
    assert store.record_path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(store.record_path.read_bytes())
    assert payload["source_database_identity"] == store.capability.source_database
    assert payload["source_identity_token"] == store.capability.source_token
    assert payload["stable_lock_identity"] is not None


def test_record_compare_and_swap_and_phase_transition(tmp_path) -> None:
    store, _ = create_store(tmp_path)
    initial = store.create()
    started = store.advance(initial, "source_create_started")
    assert started.phase == "source_create_started"
    with pytest.raises(TempPostgresRehearsalError, match="STALE"):
        store.advance(initial, "source_create_started")
    with pytest.raises(TempPostgresRehearsalError, match="PHASE_INVALID"):
        store.advance(started, "dump_started")
    with pytest.raises(TempPostgresRehearsalError, match="FACT_UPDATE_INVALID"):
        store.update_facts(started, dump_identity="a" * 64)


def test_replaced_lock_inode_fails_closed(tmp_path) -> None:
    store, _ = create_store(tmp_path)
    store.create()
    store.lock_path.unlink()
    store.lock_path.write_bytes(b"")
    with pytest.raises(TempPostgresRehearsalError, match="RECORD_INVALID"):
        store.read()


def test_resume_is_record_bound_and_cleanup_only(tmp_path) -> None:
    store, provider = create_store(tmp_path)
    record = store.create()
    record = advance_to_workflow_terminal(store, record)
    resumed = open_rehearsal_store_for_resume(
        root=store.capability.root,
        run_id=record.run_id,
        spec=spec(),
        provider=provider,
    )
    assert resumed.capability.scope == "resume_cleanup"
    assert resumed.capability.source_database == record.source_database_identity
    assert resumed.capability.source_token == record.source_identity_token
    assert resumed.capability.allowed_cleanup_targets == (
        record.replacement_database_identity,
    )
    adapter = FakeTempPostgresAdapter()
    adapter.drop(resumed.capability, record.replacement_database_identity)
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        adapter.drop(resumed.capability, record.source_database_identity)
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        resumed.create()


def test_source_cleanup_capability_requires_completed_replacement(tmp_path) -> None:
    store, provider = create_store(tmp_path)
    record = advance_to_workflow_terminal(store, store.create())
    record = store.update_facts(record, replacement_cleanup_status="completed")
    record = store.advance(record, "source_cleanup_started")
    resumed = store.issue_resume_cleanup(spec=spec(), provider=provider)
    assert resumed.allowed_cleanup_targets == (record.source_database_identity,)


def test_resume_rejects_terminal_and_server_identity_change(tmp_path) -> None:
    store, _ = create_store(tmp_path)
    record = store.create()
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        open_rehearsal_store_for_resume(
            root=store.capability.root,
            run_id=record.run_id,
            spec=spec(),
            provider=FakeObservationProvider(),
        )
    record = advance_to_workflow_terminal(store, record)
    with pytest.raises(TempPostgresRehearsalError, match="IDENTITY_MISMATCH"):
        open_rehearsal_store_for_resume(
            root=store.capability.root,
            run_id=record.run_id,
            spec=spec(),
            provider=FakeObservationProvider(role_oid=2000),
        )


def test_manual_reconciliation_is_atomic_and_blocks_resume(tmp_path) -> None:
    store, provider = create_store(tmp_path)
    record = store.create()
    record = store.advance(record, "source_create_started")
    manual = store.mark_manual_reconciliation(record, source=True)
    assert manual.phase == "manual_reconciliation_required"
    assert manual.source_cleanup_status == "manual_reconciliation"
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        open_rehearsal_store_for_resume(
            root=store.capability.root,
            run_id=record.run_id,
            spec=spec(),
            provider=provider,
        )


def test_terminal_requires_both_cleanup_and_zero_residue(tmp_path) -> None:
    store, _ = create_store(tmp_path)
    record = store.create()
    for phase in (
        "source_create_started", "source_created", "source_identity_commit_started",
        "source_identity_committed", "dump_started", "dump_committed",
        "replacement_bound", "workflow_terminal",
    ):
        record = store.advance(record, phase)
    with pytest.raises(TempPostgresRehearsalError, match="PHASE_INVALID"):
        store.advance(record, "source_cleanup_started")
    record = store.update_facts(
        record,
        replacement_cleanup_status="completed",
    )
    record = store.advance(record, "source_cleanup_started")
    record = store.update_facts(
        record, source_cleanup_status="completed", residue_count=0
    )
    record = store.advance(record, "source_cleanup_completed")
    record = store.advance(record, "rehearsal_terminal")
    assert record.phase == "rehearsal_terminal"
