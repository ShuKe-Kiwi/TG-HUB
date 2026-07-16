from __future__ import annotations

import copy
import pickle
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.deploy.backup_models import PgConnectionSpec
from app.deploy.production_recovery_postgres import (
    FakeObservationProvider,
    FakeTempPostgresAdapter,
    ProductionPostgresRecoveryAdapter,
    RealObservationProvider,
    ServerRoleObservation,
    TempPostgresCapabilityIssuer,
    TempPostgresRehearsalError,
    build_dump_command,
    build_restore_command,
    switch_database_component,
)
from app.deploy.production_recovery_postgres_models import (
    CatalogObject,
    GeneratedDatabaseFacts,
    TempPostgresRehearsalResult,
    ToolchainObservation,
    classify_empty_catalog,
    classify_restore_facts,
    validate_toolchain,
)


def spec(**changes: object) -> PgConnectionSpec:
    values: dict[str, object] = {
        "host": "127.0.0.1",
        "port": 5432,
        "user": "fixture",
        "database": "fixture_maintenance",
        "password": "secret",
    }
    values.update(changes)
    return PgConnectionSpec.model_validate(values)


def issue(tmp_path: Path):
    provider = FakeObservationProvider()
    capability = TempPostgresCapabilityIssuer().issue_initial(
        root=tmp_path / "runtime", spec=spec(), provider=provider
    )
    return capability, provider


def test_fake_provider_and_issuer_create_only_generated_identity(tmp_path) -> None:
    capability, provider = issue(tmp_path)
    assert provider.calls == 1
    assert capability.scope == "initial"
    assert capability.source_database.startswith("tg_hub_4c_source_")
    assert capability.replacement_database.startswith("tg_hub_4c_replacement_")
    assert capability.source_database != capability.replacement_database
    assert "secret" not in repr(capability)
    assert "fixture" not in repr(capability)


def test_observation_and_capability_reject_public_construction_and_copy(
    tmp_path,
) -> None:
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        ServerRoleObservation(
            server_major=16,
            server_identity_digest="a" * 64,
            role_oid=1,
            role_identity_digest="b" * 64,
            secret=object(),
        )
    capability, _ = issue(tmp_path)
    for operation in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises((pickle.PicklingError, copy.Error)):
            operation(capability)
    with pytest.raises(AttributeError, match="immutable"):
        capability.scope = "resume_cleanup"
    with pytest.raises(AttributeError, match="immutable"):
        capability.allowed_cleanup_targets = (capability.source_database,)


def test_real_provider_and_production_adapter_fail_closed(tmp_path) -> None:
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        RealObservationProvider().observe(spec())
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        ProductionPostgresRecoveryAdapter(tmp_path)


@pytest.mark.parametrize("host", ["192.0.2.1", "db.example.com", ""])
def test_connection_policy_rejects_non_loopback(tmp_path, host) -> None:
    with pytest.raises((TempPostgresRehearsalError, ValidationError)):
        TempPostgresCapabilityIssuer().issue_initial(
            root=tmp_path / "runtime",
            spec=spec(host=host),
            provider=FakeObservationProvider(),
        )


def test_restore_command_binds_generated_target_twice_and_uses_empty_env(
    tmp_path,
) -> None:
    capability, _ = issue(tmp_path)
    command = build_restore_command(
        capability,
        spec=spec(),
        pg_restore=Path("/tools/pg_restore"),
        dump=Path("/private/tmp/database.dump"),
    )
    assert command.argv.count(f"--dbname={capability.replacement_database}") == 1
    assert command.env["PGDATABASE"] == capability.replacement_database
    assert command.env["PGAPPNAME"] == "tg-hub-4c-pg-restore"
    assert "-d" not in command.argv
    assert all("://" not in item and "secret" not in item for item in command.argv)
    assert set(command.env) == {
        "PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGAPPNAME", "PGPASSWORD"
    }
    with pytest.raises(TempPostgresRehearsalError, match="CONNECTION_UNSAFE"):
        build_restore_command(
            capability,
            spec=spec(port=5433),
            pg_restore=Path("/tools/pg_restore"),
            dump=Path("/private/tmp/database.dump"),
        )


def test_dump_command_is_explicit_and_resume_cannot_restore(tmp_path) -> None:
    capability, _ = issue(tmp_path)
    command = build_dump_command(
        capability,
        spec=spec(),
        pg_dump=Path("/tools/pg_dump"),
        dump=Path("/private/tmp/database.dump"),
    )
    assert command.env["PGDATABASE"] == capability.source_database
    assert command.env["PGAPPNAME"] == "tg-hub-4c-pg-dump"
    assert "--format=custom" in command.argv


def test_fake_adapter_and_temp_config_stay_on_generated_databases(tmp_path) -> None:
    capability, _ = issue(tmp_path)
    adapter = FakeTempPostgresAdapter()
    adapter.create(capability, capability.replacement_database)
    adapter.commit_identity(capability, capability.replacement_database)
    adapter.restore(capability)
    assert adapter.inspect(capability.replacement_database).catalog_state == "complete"
    switched = switch_database_component(
        capability, spec=spec(), target="replacement"
    )
    assert switched.database == capability.replacement_database
    assert switched.host == spec().host
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        adapter.drop(capability, "production")
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        adapter.drop(capability, capability.replacement_database)


def test_symlink_temp_root_is_rejected(tmp_path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
        TempPostgresCapabilityIssuer().issue_initial(
            root=link, spec=spec(), provider=FakeObservationProvider()
        )


@pytest.mark.parametrize(
    ("facts", "expected"),
    [
        (GeneratedDatabaseFacts(exists=False), "absent"),
        (
            GeneratedDatabaseFacts(
                exists=True, identity_matches=True, catalog_state="empty"
            ),
            "restore_allowed",
        ),
        (
            GeneratedDatabaseFacts(
                exists=True, identity_matches=True, catalog_state="complete"
            ),
            "already_complete",
        ),
        (
            GeneratedDatabaseFacts(
                exists=True, identity_matches=True, catalog_state="partial"
            ),
            "partial",
        ),
        (
            GeneratedDatabaseFacts(
                exists=True, identity_matches=False, catalog_state="empty"
            ),
            "manual_reconciliation",
        ),
    ],
)
def test_restore_fact_classification(facts, expected) -> None:
    assert classify_restore_facts(facts) == expected


def test_empty_catalog_uses_fixed_allowlist() -> None:
    allowed = (
        CatalogObject(
            object_type="schema",
            schema_name="public",
            name="public",
            user_owned=True,
        ),
        CatalogObject(
            object_type="relation",
            schema_name="pg_catalog",
            name="pg_class",
            user_owned=False,
        ),
    )
    assert classify_empty_catalog(allowed) == "empty"
    extra = allowed + (
        CatalogObject(
            object_type="relation",
            schema_name="public",
            name="unexpected",
            user_owned=True,
        ),
    )
    assert classify_empty_catalog(extra) == "partial"


def test_toolchain_requires_matching_tools_not_older_than_server() -> None:
    validate_toolchain(
        ToolchainObservation(server_major=16, pg_dump_major=16, pg_restore_major=16)
    )
    with pytest.raises(ValueError, match="TOOL_VERSION_UNSUPPORTED"):
        validate_toolchain(
            ToolchainObservation(
                server_major=16,
                pg_dump_major=15,
                pg_restore_major=15,
            )
        )
    with pytest.raises(ValueError, match="TOOL_VERSION_UNSUPPORTED"):
        validate_toolchain(
            ToolchainObservation(
                server_major=16,
                pg_dump_major=16,
                pg_restore_major=17,
            )
        )


def test_success_result_requires_full_rehearsal_terminal() -> None:
    with pytest.raises(ValidationError):
        TempPostgresRehearsalResult(
            status="success",
            run_id="a" * 32,
            phase="workflow_terminal",
            source_created=True,
            dump_validated=True,
            replacement_created=True,
            restore_completed=True,
            verification_completed=True,
            config_switched=True,
            rollback_completed=True,
            replacement_cleanup_completed=True,
            source_cleanup_completed=False,
            residue_count=1,
        )
