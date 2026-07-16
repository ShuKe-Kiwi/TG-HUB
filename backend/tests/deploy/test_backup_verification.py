from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone

import pytest

from app.deploy.backup_models import (
    BackupExclusions,
    BackupManifest,
    DatabaseBackupManifest,
    WatchlistBackupManifest,
)
from app.deploy.backup_verification import BackupVerificationStore
from app.deploy.backup_verification import BackupVerificationError

BACKUP_ID = "20260714T120000.123456Z-0123456789abcdef0123456789abcdef"


def _manifest() -> BackupManifest:
    return BackupManifest(
        backup_id=BACKUP_ID,
        created_at_utc=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        app_git_commit="a" * 40,
        git_worktree_clean=True,
        python_version="3.12.8",
        dependency_lock_sha256="b" * 64,
        alembic_revision="head",
        config_schema_version=1,
        database=DatabaseBackupManifest(
            sha256="c" * 64,
            size_bytes=10,
            source_server_version="16.4",
            source_server_major=16,
            pg_dump_version="pg_dump (PostgreSQL) 16.4",
            pg_dump_major=16,
        ),
        watchlist=WatchlistBackupManifest(
            sha256="d" * 64,
            size_bytes=20,
            revision="revision",
        ),
        exclusions=BackupExclusions(
            production_env="secret_material_excluded",
            telethon_session="authentication_session_excluded",
            logs="operational_data_excluded",
            runtime_state="ephemeral_data_excluded",
        ),
    )


def test_verification_store_writes_private_atomic_sidecar_and_observes_it(
    tmp_path,
) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    manifest = _manifest()
    manifest_hash = hashlib.sha256(b"manifest").hexdigest()
    store = BackupVerificationStore(root)

    written = store.write_passed(
        manifest=manifest,
        manifest_sha256=manifest_hash,
        verified_at_utc=datetime(2026, 7, 14, 13, tzinfo=timezone.utc),
    )
    observed = store.observe(manifest=manifest, manifest_sha256=manifest_hash)
    path = store.root / f"{BACKUP_ID}.json"

    assert written.backup_id == BACKUP_ID
    assert observed.status == "valid"
    assert observed.restore_verified == "yes"
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_verification_store_maps_missing_and_identity_mismatch(tmp_path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    manifest = _manifest()
    store = BackupVerificationStore(root)

    missing = store.observe(manifest=manifest, manifest_sha256="e" * 64)
    store.write_passed(manifest=manifest, manifest_sha256="e" * 64)
    mismatched = store.observe(manifest=manifest, manifest_sha256="f" * 64)

    assert missing.status == "missing"
    assert mismatched.status == "invalid"
    assert mismatched.error_code == "BACKUP_VERIFICATION_IDENTITY_INVALID"


def test_exact_existing_sidecar_is_confirmed_without_rewrite(tmp_path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    manifest = _manifest()
    store = BackupVerificationStore(root)
    first = store.write_passed(
        manifest=manifest,
        manifest_sha256="e" * 64,
        verified_at_utc=datetime(2026, 7, 14, 13, tzinfo=timezone.utc),
    )
    path = store.root / f"{BACKUP_ID}.json"
    original_mtime = path.stat().st_mtime_ns
    os.utime(path, ns=(original_mtime, original_mtime))

    confirmed = store.write_passed(
        manifest=manifest,
        manifest_sha256="e" * 64,
        verified_at_utc=datetime(2026, 7, 14, 14, tzinfo=timezone.utc),
    )

    assert confirmed.verified_at_utc == first.verified_at_utc
    assert path.stat().st_mtime_ns == original_mtime


def test_invalid_existing_sidecar_is_not_overwritten(tmp_path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    store = BackupVerificationStore(root)
    store.root.mkdir(mode=0o700)
    path = store.root / f"{BACKUP_ID}.json"
    original = b"invalid evidence\n"
    path.write_bytes(original)
    path.chmod(0o600)

    with pytest.raises(BackupVerificationError) as raised:
        store.write_passed(manifest=_manifest(), manifest_sha256="e" * 64)

    assert raised.value.error_code == "BACKUP_VERIFICATION_IDENTITY_INVALID"
    assert path.read_bytes() == original


def test_verification_store_rejects_unsupported_newer_schema(tmp_path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    store = BackupVerificationStore(root)
    store.root.mkdir(mode=0o700)
    path = store.root / f"{BACKUP_ID}.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "backup_id": BACKUP_ID,
                "verified_at_utc": "2026-07-14T13:00:00Z",
                "verification_version": 2,
                "manifest_sha256": "a" * 64,
                "database_dump_sha256": "c" * 64,
                "watchlist_snapshot_sha256": "d" * 64,
                "result": "passed",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    observed = store.observe(manifest=_manifest(), manifest_sha256="a" * 64)

    assert observed.status == "invalid"
    assert observed.restore_verified == "no"


def test_verification_store_rejects_valid_sidecar_with_open_permissions(
    tmp_path,
) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    store = BackupVerificationStore(root)
    manifest = _manifest()
    store.write_passed(manifest=manifest, manifest_sha256="a" * 64)
    path = store.root / f"{BACKUP_ID}.json"
    path.chmod(0o644)

    observed = store.observe(manifest=manifest, manifest_sha256="a" * 64)

    assert observed.status == "invalid"
    assert observed.restore_verified == "no"
    assert observed.error_code == "BACKUP_VERIFICATION_IDENTITY_INVALID"
