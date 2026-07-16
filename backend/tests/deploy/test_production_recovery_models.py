from __future__ import annotations

import pytest

from app.deploy.production_recovery_models import (
    AuthorizationObservations,
    ProductionRecoveryRecord,
    ResourceIdentities,
    advance_production_recovery,
)


BACKUP_ID = "20260716T120000.000000Z-" + "a" * 32


def make_record(**updates: object) -> ProductionRecoveryRecord:
    values: dict[str, object] = dict(
        incident_id="b" * 32,
        selected_backup_id=BACKUP_ID,
        resources=ResourceIdentities(
            original_database_revision="rev1",
            original_database_component="tg_hub",
            original_database_identity="original",
            original_database_owner="owner",
            replacement_database_identity="replacement",
            replacement_identity_token="token",
            expected_owner_identity="owner",
            selected_manifest_sha256="0" * 64,
            selected_database_dump_sha256="4" * 64,
            selected_watchlist_sha256="5" * 64,
            original_env_sha256="1" * 64,
            staged_env_sha256="2" * 64,
            original_watchlist_sha256="3" * 64,
        ),
    )
    values.update(updates)
    return ProductionRecoveryRecord.model_validate(values)


def test_backup_id_is_not_a_path_url_or_database_name() -> None:
    for value in ("/tmp/backup", "https://example.test/x", "tg_hub"):
        with pytest.raises(ValueError, match="canonical backup"):
            make_record(selected_backup_id=value)


def test_forward_and_skip_branches_are_explicit() -> None:
    assert (
        advance_production_recovery(make_record(), "protection_backup_started").phase
        == "protection_backup_started"
    )
    skipped = make_record(
        protection_backup_status="skipped_authorized",
        authorizations=AuthorizationObservations(protection_skip_authorized="yes"),
    )
    assert (
        advance_production_recovery(
            skipped, "protection_backup_skipped_authorized"
        ).phase
        == "protection_backup_skipped_authorized"
    )
    with pytest.raises(ValueError, match="SKIP_NOT_AUTHORIZED"):
        advance_production_recovery(make_record(), "protection_backup_skipped_authorized")
    with pytest.raises(ValueError, match="PHASE_INVALID"):
        advance_production_recovery(make_record(), "replacement_create_started")


def test_verification_and_service_stop_guards() -> None:
    record = make_record(phase="verification_started")
    with pytest.raises(ValueError, match="NOT_PASSED"):
        advance_production_recovery(record, "verification_completed")
    stopped = make_record(
        phase="services_stop_started",
        monitor_stopped="yes",
        session_lease_free="yes",
        application_stopped="yes",
    )
    with pytest.raises(ValueError, match="SERVICES_NOT_STOPPED"):
        advance_production_recovery(stopped, "services_stopped")


def test_watchlist_and_rollback_branches_do_not_invent_phases() -> None:
    no_switch = make_record(phase="env_switched", watchlist_switch_authorized="no")
    assert advance_production_recovery(no_switch, "config_switched").phase == "config_switched"
    with pytest.raises(ValueError, match="PHASE_INVALID"):
        advance_production_recovery(no_switch, "watchlist_switch_started")
    rollback = make_record(
        phase="rollback_env_completed",
        watchlist_was_switched="no",
        replacement_activated="yes",
    )
    assert (
        advance_production_recovery(
            rollback, "rollback_application_start_started"
        ).phase
        == "rollback_application_start_started"
    )


def test_monitor_start_is_permanent_rollback_fence() -> None:
    with pytest.raises(ValueError, match="PHASE_INVALID"):
        advance_production_recovery(
            make_record(phase="monitor_start_started", replacement_activated="yes"),
            "rollback_started",
        )


def test_completion_requires_bounded_observation_and_no_manual_reconciliation() -> None:
    with pytest.raises(ValueError, match="COMPLETION_INVALID"):
        advance_production_recovery(
            make_record(phase="monitor_started", replacement_activated="yes"),
            "completed",
        )
    complete = make_record(
        phase="monitor_started",
        replacement_activated="yes",
        monitor_first_write_observed="no",
    )
    assert advance_production_recovery(complete, "completed").phase == "completed"


def test_switch_and_monitor_phases_require_durable_facts() -> None:
    with pytest.raises(ValueError, match="REPLACEMENT_NOT_ACTIVATED"):
        advance_production_recovery(
            make_record(phase="env_switch_started"), "env_switched"
        )
    with pytest.raises(ValueError, match="MONITOR_START_NOT_AUTHORIZED"):
        advance_production_recovery(
            make_record(phase="readiness_passed"), "monitor_start_authorized"
        )
    authorized = make_record(
        phase="monitor_start_authorized",
        authorizations=AuthorizationObservations(monitor_start_authorized="yes"),
    )
    with pytest.raises(ValueError, match="MONITOR_BASELINE_MISSING"):
        advance_production_recovery(authorized, "monitor_start_started")
