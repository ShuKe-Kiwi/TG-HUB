from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.deploy.backup_models import (
    BACKUP_ERROR_CODES,
    BackupExclusions,
    BackupManifest,
    BackupRunResult,
    DatabaseBackupManifest,
    RestoreRecoveryRecord,
    WatchlistBackupManifest,
    advance_recovery_phase,
    generate_restore_identity,
    require_backup_version_compatibility,
    require_restore_version_compatibility,
    validate_backup_id,
    validate_restore_target_name,
)

BACKUP_ID = "20260714T120000.123456Z-0123456789abcdef0123456789abcdef"
SHA256 = "a" * 64


def _database(**overrides: object) -> DatabaseBackupManifest:
    values: dict[str, object] = {
        "sha256": SHA256,
        "size_bytes": 42,
        "source_server_version": "PostgreSQL 16.4",
        "source_server_major": 16,
        "pg_dump_version": "pg_dump (PostgreSQL) 16.4",
        "pg_dump_major": 16,
    }
    values.update(overrides)
    return DatabaseBackupManifest(**values)


def _manifest(**overrides: object) -> BackupManifest:
    values: dict[str, object] = {
        "backup_id": BACKUP_ID,
        "created_at_utc": datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        "app_git_commit": "b" * 40,
        "git_worktree_clean": True,
        "python_version": "3.12.8",
        "dependency_lock_sha256": "c" * 64,
        "alembic_revision": "20260714_raw_message_restrict",
        "config_schema_version": 1,
        "database": _database(),
        "watchlist": WatchlistBackupManifest(
            sha256="d" * 64,
            size_bytes=24,
            revision="revision-1",
        ),
        "exclusions": BackupExclusions(
            production_env="secret_material_excluded",
            telethon_session="authentication_session_excluded",
            logs="operational_data_excluded",
            runtime_state="ephemeral_data_excluded",
        ),
    }
    values.update(overrides)
    return BackupManifest(**values)


def _recovery_record(phase: str = "planned") -> RestoreRecoveryRecord:
    return RestoreRecoveryRecord(
        opaque_id="e" * 32,
        generated_target_name="tg_hub_restore_verify_20260714T120000Z_0123456789abcdef",
        identity_token="f" * 64,
        created_at=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        backup_id=BACKUP_ID,
        phase=phase,
    )


def test_manifest_accepts_locked_complete_schema() -> None:
    manifest = _manifest()

    assert manifest.schema_version == 1
    assert manifest.backup_status == "complete"
    assert manifest.database.filename == "database.dump"
    assert manifest.watchlist.filename == "watchlist.json"


@pytest.mark.parametrize(
    "override",
    [
        {"unknown": "field"},
        {"backup_id": "../escape"},
        {"created_at_utc": datetime(2026, 7, 14, 12)},
        {
            "created_at_utc": datetime(
                2026, 7, 14, 12, tzinfo=timezone(timedelta(hours=8))
            )
        },
        {"app_git_commit": "not-a-commit"},
        {"dependency_lock_sha256": "ABC"},
        {"git_worktree_clean": False},
    ],
)
def test_manifest_rejects_noncanonical_or_unlocked_values(
    override: dict[str, object],
) -> None:
    with pytest.raises((ValidationError, ValueError)):
        _manifest(**override)


def test_manifest_rejects_source_dump_major_mismatch() -> None:
    with pytest.raises(ValidationError, match="PG_DUMP_VERSION_UNSUPPORTED"):
        _manifest(database=_database(pg_dump_major=15))


@pytest.mark.parametrize(
    "value",
    ["", "../backup", f"{BACKUP_ID}/extra", BACKUP_ID.upper()],
)
def test_backup_id_rejects_paths_and_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="invalid backup id"):
        validate_backup_id(value)


def test_backup_result_maps_success_and_uncertain_final_package() -> None:
    success = BackupRunResult(
        status="pass",
        backup_id=BACKUP_ID,
        package_state="final_committed",
        manifest_valid="yes",
        database_dump_valid="yes",
        watchlist_snapshot_valid="yes",
        backup_bytes=100,
    )
    uncertain = BackupRunResult(
        status="fail",
        backup_id=BACKUP_ID,
        package_state="commit_uncertain",
        manifest_valid="yes",
        database_dump_valid="yes",
        watchlist_snapshot_valid="yes",
        backup_bytes=100,
        error_code="BACKUP_COMMIT_UNCERTAIN",
    )

    assert success.package_state == "final_committed"
    assert uncertain.backup_id == BACKUP_ID
    assert uncertain.error_code in BACKUP_ERROR_CODES


@pytest.mark.parametrize(
    "values",
    [
        {"status": "pass", "backup_id": None, "package_state": "final_committed"},
        {"status": "fail", "backup_id": BACKUP_ID, "package_state": "temp_only"},
        {"status": "fail", "backup_id": BACKUP_ID, "package_state": "commit_uncertain"},
        {
            "status": "fail",
            "backup_id": BACKUP_ID,
            "package_state": "final_committed",
        },
    ],
)
def test_backup_result_rejects_invalid_commit_mapping(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        BackupRunResult(
            manifest_valid="no",
            database_dump_valid="no",
            watchlist_snapshot_valid="no",
            **values,
        )


def test_version_matrix_requires_all_postgres_majors_to_match() -> None:
    require_backup_version_compatibility(
        source_server_major=16,
        pg_dump_major=16,
    )
    require_restore_version_compatibility(
        source_server_major=16,
        pg_dump_major=16,
        pg_restore_major=16,
        restore_target_server_major=16,
    )

    with pytest.raises(ValueError, match="PG_DUMP_VERSION_UNSUPPORTED"):
        require_backup_version_compatibility(
            source_server_major=16,
            pg_dump_major=17,
        )
    with pytest.raises(ValueError, match="PG_RESTORE_VERSION_UNSUPPORTED"):
        require_restore_version_compatibility(
            source_server_major=16,
            pg_dump_major=16,
            pg_restore_major=17,
            restore_target_server_major=16,
        )


def test_restore_identity_is_random_canonical_and_contains_no_process_data() -> None:
    created_at = datetime(
        2026,
        7,
        14,
        20,
        tzinfo=timezone(timedelta(hours=8)),
    )
    first = generate_restore_identity(created_at)
    second = generate_restore_identity(created_at)

    assert first != second
    assert first[0].startswith("tg_hub_restore_verify_20260714T120000Z_")
    validate_restore_target_name(first[0])
    assert len(first[1]) == 32
    assert len(first[2]) == 64


@pytest.mark.parametrize(
    "value",
    [
        "production",
        "tg_hub_restore_verify_bad",
        "tg_hub_restore_verify_20260714T120000Z_0123456789abcdef;DROP",
        'tg_hub_restore_verify_20260714T120000Z_0123456789abcde"',
    ],
)
def test_restore_target_rejects_unsafe_sql_identifier_input(value: str) -> None:
    with pytest.raises(ValueError, match="unsafe restore target name"):
        validate_restore_target_name(value)


def test_recovery_record_is_strict_and_phase_transitions_are_forward_only() -> None:
    planned = _recovery_record()
    create_started = advance_recovery_phase(planned, "create_started")
    created = advance_recovery_phase(create_started, "database_created")
    commit_started = advance_recovery_phase(created, "identity_commit_started")
    committed = advance_recovery_phase(commit_started, "identity_committed")
    started = advance_recovery_phase(committed, "restore_started")
    failed = advance_recovery_phase(started, "restore_failed")

    assert failed.phase == "restore_failed"
    with pytest.raises(ValueError, match="invalid recovery phase transition"):
        advance_recovery_phase(failed, "restore_started")
    with pytest.raises(ValidationError):
        RestoreRecoveryRecord(**{**planned.model_dump(), "host": "localhost"})

    passed = advance_recovery_phase(started, "verification_passed")
    assert advance_recovery_phase(passed, "drop_failed").phase == "drop_failed"
    with pytest.raises(ValueError, match="invalid recovery phase transition"):
        advance_recovery_phase(failed, "drop_failed")


def test_recovery_record_rejects_invalid_token_target_and_backup_id() -> None:
    payload = _recovery_record().model_dump()
    for key, value in (
        ("identity_token", "secret"),
        ("generated_target_name", "tg_hub"),
        ("backup_id", "../backup"),
    ):
        with pytest.raises(ValidationError):
            RestoreRecoveryRecord(**{**payload, key: value})
