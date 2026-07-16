from __future__ import annotations

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.deploy import backup_retention
from app.deploy.backup_retention import create_temp_mutation_capability
from app.deploy.backup_retention_authorization import (
    AuthorizationError,
    AuthorizationStore,
    PinPayload,
    PinReconcilePayload,
    ProductionAuthorization,
    ProductionMutationCommandAdapter,
    RetentionApplyPayload,
    RetentionResumePayload,
    TempAuthorizationIssuer,
    _authorization_identity,
)

UTC = timezone.utc
NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
BACKUP_A = "20260716T120000.000000Z-" + "1" * 32
BACKUP_B = "20260715T120000.000000Z-" + "2" * 32
PLAN_ID = "a" * 32
JOURNAL_DIGEST = "b" * 64
PLAN_DIGEST = "c" * 64


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700, parents=True)
    return Settings(BACKUP_DIR=root)


def _issuer(tmp_path: Path):
    settings = _settings(tmp_path)
    capability = create_temp_mutation_capability(settings.BACKUP_DIR)
    issuer = TempAuthorizationIssuer(settings, capability, clock=lambda: NOW)
    return settings, issuer, issuer.store


def _pin_payload() -> PinPayload:
    return PinPayload(
        backup_id=BACKUP_A,
        reason_code="pre_upgrade",
        expected_pin_identity="missing",
    )


def _apply_payload() -> RetentionApplyPayload:
    return RetentionApplyPayload(
        plan_id=PLAN_ID,
        plan_digest=PLAN_DIGEST,
        ordered_candidate_backup_ids=(BACKUP_A, BACKUP_B),
        expected_candidate_count=2,
        expected_reclaim_bytes=123,
        acknowledge_non_transactional_multi_package_delete=True,
        acknowledge_terminal_metadata_retained=True,
    )


def test_issue_consume_is_single_use_and_preserves_stable_lock_inode(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )
    lock_path = store.root / f"{authorization.authorization_id}.lock"
    before = lock_path.stat()
    journal = store.root / "prepared.marker"

    with store.consume(
        authorization.authorization_id,
        nonce,
        prepare_journal=lambda _: journal.write_text("planned", encoding="utf-8"),
        clock=lambda: NOW,
    ) as consumed:
        assert journal.read_text(encoding="utf-8") == "planned"
        assert consumed.phase == "consumed"

    after = lock_path.stat()
    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert store.read(authorization.authorization_id).phase == "consumed"
    with pytest.raises(AuthorizationError, match="ALREADY_CONSUMED"):
        with store.consume(
            authorization.authorization_id,
            nonce,
            prepare_journal=lambda _: None,
            clock=lambda: NOW,
        ):
            pass


def test_prepare_failure_does_not_consume_authorization(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )

    with pytest.raises(RuntimeError, match="prepare failed"):
        with store.consume(
            authorization.authorization_id,
            nonce,
            prepare_journal=lambda _: (_ for _ in ()).throw(
                RuntimeError("prepare failed")
            ),
            clock=lambda: NOW,
        ):
            pass

    assert store.read(authorization.authorization_id).phase == "issued"


def test_mutation_failure_keeps_authorization_consumed(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )

    with pytest.raises(RuntimeError, match="mutation failed"):
        with store.consume(
            authorization.authorization_id,
            nonce,
            prepare_journal=lambda _: None,
            clock=lambda: NOW,
        ):
            raise RuntimeError("mutation failed")

    assert store.read(authorization.authorization_id).phase == "consumed"


def test_nonce_expiry_and_cancel_fail_closed(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=1)
    )
    with pytest.raises(AuthorizationError, match="NONCE_INVALID"):
        with store.consume(
            authorization.authorization_id,
            "0" * 64,
            prepare_journal=lambda _: None,
            clock=lambda: NOW,
        ):
            pass
    with pytest.raises(AuthorizationError, match="EXPIRED"):
        with store.consume(
            authorization.authorization_id,
            nonce,
            prepare_journal=lambda _: None,
            clock=lambda: NOW + timedelta(minutes=2),
        ):
            pass
    cancelled = store.cancel(authorization.authorization_id, nonce)
    assert cancelled.status == "pass"
    assert cancelled.phase == "cancelled"
    assert store.cancel(authorization.authorization_id, nonce).error_code == (
        "BACKUP_AUTHORIZATION_ALREADY_CONSUMED"
    )


def test_expiry_is_rechecked_after_backup_lock_acquisition(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=1)
    )
    times = iter((NOW, NOW + timedelta(minutes=2)))
    prepared = False

    def prepare(_: ProductionAuthorization) -> None:
        nonlocal prepared
        prepared = True

    with pytest.raises(AuthorizationError, match="EXPIRED"):
        with store.consume(
            authorization.authorization_id,
            nonce,
            prepare_journal=prepare,
            clock=lambda: next(times),
        ):
            pass
    assert prepared is False
    assert store.read(authorization.authorization_id).phase == "issued"


def test_concurrent_consumption_has_one_winner(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )

    def consume() -> str:
        try:
            with store.consume(
                authorization.authorization_id,
                nonce,
                prepare_journal=lambda _: None,
                clock=lambda: NOW,
            ):
                return "winner"
        except AuthorizationError as exc:
            return exc.error_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: consume(), range(2)))

    assert outcomes.count("winner") == 1
    assert outcomes.count("BACKUP_AUTHORIZATION_ALREADY_CONSUMED") == 1


def test_strict_payload_rejects_missing_unknown_and_cross_operation_fields() -> None:
    base = _apply_payload().model_dump(mode="json")
    missing = dict(base)
    missing.pop("acknowledge_terminal_metadata_retained")
    with pytest.raises(ValidationError):
        RetentionApplyPayload.model_validate(missing)
    with pytest.raises(ValidationError):
        RetentionApplyPayload.model_validate({**base, "unknown": True})

    root_identity = {
        "configured_logical_root_id": "d" * 64,
        "resolved_device": 1,
        "resolved_inode": 2,
    }
    with pytest.raises(ValidationError):
        ProductionAuthorization.model_validate(
            {
                "authorization_id": "e" * 32,
                "authorization_nonce": "f" * 64,
                "operation": "pin",
                "backup_root_identity": root_identity,
                "issued_at_utc": NOW,
                "expires_at_utc": NOW + timedelta(minutes=1),
                "payload": base,
            }
        )


def test_pin_reconcile_requires_exact_consumed_predecessor(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    predecessor, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )
    with store.consume(
        predecessor.authorization_id,
        nonce,
        prepare_journal=lambda _: None,
        clock=lambda: NOW,
    ):
        pass
    predecessor = store.read(predecessor.authorization_id)
    payload = PinReconcilePayload(
        predecessor_authorization_id=predecessor.authorization_id,
        predecessor_consumed_identity=_authorization_identity(predecessor),
        original_operation="pin",
        pin_operation_id="1" * 32,
        pin_journal_digest="2" * 64,
        pin_journal_phase="mutation_started",
        backup_id=BACKUP_A,
        intended_reason_code="pre_upgrade",
        observed_before_pin_identity="missing",
        intended_result_pin_identity="3" * 64,
        expected_current_pin_identity="missing",
        reconcile_scope="settle_current_only",
        acknowledge_current_partial_state=True,
    )
    reconcile, reconcile_nonce = issuer.issue(
        payload, expires_at_utc=NOW + timedelta(minutes=5)
    )
    with store.consume(
        reconcile.authorization_id,
        reconcile_nonce,
        prepare_journal=lambda _: None,
        clock=lambda: NOW,
    ) as consumed:
        assert consumed.operation == "pin_reconcile"


def test_pin_reconcile_rejects_wrong_predecessor_identity_before_prepare(
    tmp_path,
) -> None:
    _, issuer, store = _issuer(tmp_path)
    predecessor, nonce = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )
    with store.consume(
        predecessor.authorization_id,
        nonce,
        prepare_journal=lambda _: None,
        clock=lambda: NOW,
    ):
        pass
    payload = PinReconcilePayload(
        predecessor_authorization_id=predecessor.authorization_id,
        predecessor_consumed_identity="0" * 64,
        original_operation="pin",
        pin_operation_id="1" * 32,
        pin_journal_digest="2" * 64,
        pin_journal_phase="mutation_started",
        backup_id=BACKUP_A,
        intended_reason_code="pre_upgrade",
        observed_before_pin_identity="missing",
        intended_result_pin_identity="3" * 64,
        expected_current_pin_identity="missing",
        reconcile_scope="settle_current_only",
        acknowledge_current_partial_state=True,
    )
    reconcile, reconcile_nonce = issuer.issue(
        payload, expires_at_utc=NOW + timedelta(minutes=5)
    )
    prepared = False

    def prepare(_: ProductionAuthorization) -> None:
        nonlocal prepared
        prepared = True

    with pytest.raises(AuthorizationError, match="PREDECESSOR_INVALID"):
        with store.consume(
            reconcile.authorization_id,
            reconcile_nonce,
            prepare_journal=prepare,
            clock=lambda: NOW,
        ):
            pass
    assert prepared is False
    assert store.read(reconcile.authorization_id).phase == "issued"


def test_retention_resume_requires_predecessor_lineage(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    predecessor, nonce = issuer.issue(
        _apply_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )
    with store.consume(
        predecessor.authorization_id,
        nonce,
        prepare_journal=lambda _: None,
        clock=lambda: NOW,
    ):
        pass
    predecessor = store.read(predecessor.authorization_id)
    payload = RetentionResumePayload(
        predecessor_authorization_id=predecessor.authorization_id,
        predecessor_consumed_identity=_authorization_identity(predecessor),
        plan_id=PLAN_ID,
        plan_digest=PLAN_DIGEST,
        journal_digest=JOURNAL_DIGEST,
        journal_generation=2,
        already_deleted_backup_ids=(BACKUP_A,),
        earliest_unfinished_backup_id=BACKUP_B,
        earliest_unfinished_phase="cleanup_required",
        remaining_ordered_candidate_ids=(BACKUP_B,),
        resume_scope="settle_current_only",
        already_reclaimed_bytes=60,
        remaining_expected_reclaim_bytes=63,
        acknowledge_current_partial_state=True,
    )
    resume, resume_nonce = issuer.issue(
        payload, expires_at_utc=NOW + timedelta(minutes=5)
    )
    with store.consume(
        resume.authorization_id,
        resume_nonce,
        prepare_journal=lambda _: None,
        clock=lambda: NOW,
    ) as consumed:
        assert consumed.operation == "retention_resume"


def test_retention_resume_cannot_switch_predecessor_plan(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    predecessor, nonce = issuer.issue(
        _apply_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )
    with store.consume(
        predecessor.authorization_id,
        nonce,
        prepare_journal=lambda _: None,
        clock=lambda: NOW,
    ):
        pass
    predecessor = store.read(predecessor.authorization_id)
    payload = RetentionResumePayload(
        predecessor_authorization_id=predecessor.authorization_id,
        predecessor_consumed_identity=_authorization_identity(predecessor),
        plan_id="d" * 32,
        plan_digest=PLAN_DIGEST,
        journal_digest=JOURNAL_DIGEST,
        journal_generation=2,
        already_deleted_backup_ids=(BACKUP_A,),
        earliest_unfinished_backup_id=BACKUP_B,
        earliest_unfinished_phase="cleanup_required",
        remaining_ordered_candidate_ids=(BACKUP_B,),
        resume_scope="settle_current_only",
        already_reclaimed_bytes=60,
        remaining_expected_reclaim_bytes=63,
        acknowledge_current_partial_state=True,
    )
    resume, resume_nonce = issuer.issue(
        payload, expires_at_utc=NOW + timedelta(minutes=5)
    )

    with pytest.raises(AuthorizationError, match="PREDECESSOR_INVALID"):
        with store.consume(
            resume.authorization_id,
            resume_nonce,
            prepare_journal=lambda _: None,
            clock=lambda: NOW,
        ):
            pass


def test_missing_or_replaced_stable_lock_is_rejected(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, _ = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )
    lock_path = store.root / f"{authorization.authorization_id}.lock"
    lock_path.unlink()
    lock_path.symlink_to(store.root / f"{authorization.authorization_id}.json")

    with pytest.raises(AuthorizationError, match="LOCK_INVALID"):
        store.read(authorization.authorization_id)


def test_regular_stable_lock_inode_replacement_is_rejected(tmp_path) -> None:
    _, issuer, store = _issuer(tmp_path)
    authorization, _ = issuer.issue(
        _pin_payload(), expires_at_utc=NOW + timedelta(minutes=5)
    )
    lock_path = store.root / f"{authorization.authorization_id}.lock"
    replacement = store.root / "replacement.lock"
    replacement.write_bytes(b"")
    replacement.chmod(0o600)
    replacement.replace(lock_path)

    with pytest.raises(AuthorizationError, match="LOCK_INVALID"):
        store.read(authorization.authorization_id)


def test_orphan_stable_lock_cannot_be_reused_for_authorization(tmp_path) -> None:
    _, _, store = _issuer(tmp_path)
    authorization_id = "4" * 32
    store.root.mkdir(mode=0o700, parents=True, exist_ok=True)
    orphan = store.root / f"{authorization_id}.lock"
    orphan.write_bytes(b"")
    orphan.chmod(0o600)

    with pytest.raises(AuthorizationError, match="ALREADY_EXISTS"):
        store.initialize_stable_lock(authorization_id)


def test_production_command_surface_rejects_before_settings_load(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        backup_retention,
        "load_settings",
        lambda: (_ for _ in ()).throw(AssertionError("settings must not load")),
    )

    code = asyncio.run(backup_retention._main(["apply", "--plan-id", PLAN_ID]))

    assert code == 1
    assert "BACKUP_AUTHORIZATION_ISSUER_UNAVAILABLE" in capsys.readouterr().out
    assert ProductionMutationCommandAdapter.reject().mutation_status == "not_started"
