from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.deploy.backup_inventory import BackupInventoryService
from app.deploy.backup_models import (
    BackupExclusions,
    BackupManifest,
    BackupPinSidecar,
    DatabaseBackupManifest,
    WatchlistBackupManifest,
)
from app.deploy.backup_service import ToolResult
from app.deploy.backup_verification import BackupVerificationStore
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.watchlist_service import watchlist_revision

BACKUP_ID = "20260714T120000.123456Z-0123456789abcdef0123456789abcdef"


class FakeRunner:
    async def run(self, argv, *, env, cwd=None):
        values = [str(value) for value in argv]
        if "--version" in values:
            return ToolResult(0, b"pg_restore (PostgreSQL) 16.4\n", b"")
        tables = [
            "alembic_version", "channels", "raw_messages", "works",
            "resources", "resource_links", "resource_sources",
        ]
        lines = []
        for index, table in enumerate(tables, 1):
            lines.append(f"{index}; 1259 1 TABLE public {table} owner")
            if table != "alembic_version":
                lines.append(f"{index + 20}; 0 1 TABLE DATA public {table} owner")
        return ToolResult(0, ("\n".join(lines) + "\n").encode(), b"")


def _package(tmp_path: Path) -> tuple[Settings, BackupManifest]:
    root = tmp_path / "backups"
    package = root / BACKUP_ID
    package.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    dump = b"PGDMP-fake"
    watch = json.dumps(
        {
            "source_channels": [{"ref": "https://t.me/example", "enabled": True}],
            "watch_titles": [{"title": "百花杀", "enabled": True, "aliases": []}],
        },
        ensure_ascii=False,
    ).encode()
    config = WatchlistConfig.model_validate_json(watch)
    manifest = BackupManifest(
        backup_id=BACKUP_ID,
        created_at_utc=datetime(2026, 7, 14, 12, tzinfo=timezone.utc),
        app_git_commit="a" * 40,
        git_worktree_clean=True,
        python_version="3.12.8",
        dependency_lock_sha256="b" * 64,
        alembic_revision="head",
        config_schema_version=1,
        database=DatabaseBackupManifest(
            sha256=hashlib.sha256(dump).hexdigest(),
            size_bytes=len(dump),
            source_server_version="16.4",
            source_server_major=16,
            pg_dump_version="pg_dump (PostgreSQL) 16.4",
            pg_dump_major=16,
        ),
        watchlist=WatchlistBackupManifest(
            sha256=hashlib.sha256(watch).hexdigest(),
            size_bytes=len(watch),
            revision=watchlist_revision(config),
        ),
        exclusions=BackupExclusions(
            production_env="secret_material_excluded",
            telethon_session="authentication_session_excluded",
            logs="operational_data_excluded",
            runtime_state="ephemeral_data_excluded",
        ),
    )
    (package / "database.dump").write_bytes(dump)
    (package / "watchlist.json").write_bytes(watch)
    (package / "manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    for path in package.iterdir():
        path.chmod(0o600)
    return Settings(BACKUP_DIR=root), manifest


async def test_inventory_projects_valid_missing_verification_package(tmp_path) -> None:
    settings, _ = _package(tmp_path)

    result = await BackupInventoryService(
        settings,
        runner=FakeRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).inventory()

    assert result.status == "pass"
    assert result.package_count == 1
    assert result.valid_count == 1
    assert result.restore_verified_count == 0
    assert result.entries[0].verification_status == "missing"
    assert result.entries[0].retention_disposition == "keep"


async def test_inventory_projects_valid_verification_and_read_only_pin(tmp_path) -> None:
    settings, manifest = _package(tmp_path)
    package = settings.BACKUP_DIR / BACKUP_ID
    manifest_hash = hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    BackupVerificationStore(settings.BACKUP_DIR).write_passed(
        manifest=manifest, manifest_sha256=manifest_hash
    )
    pin_root = settings.BACKUP_DIR / ".pins"
    pin_root.mkdir(mode=0o700)
    pin = BackupPinSidecar(
        backup_id=BACKUP_ID,
        pinned_at_utc=datetime(2026, 7, 14, 13, tzinfo=timezone.utc),
        reason_code="pre_upgrade",
    )
    pin_path = pin_root / f"{BACKUP_ID}.json"
    pin_path.write_text(pin.model_dump_json(), encoding="utf-8")
    pin_path.chmod(0o600)

    result = await BackupInventoryService(
        settings,
        runner=FakeRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).inventory()

    item = result.entries[0]
    assert item.restore_verified == "yes"
    assert item.verification_status == "valid"
    assert item.pinned == "yes"
    assert item.retention_disposition == "protected"
    assert result.restore_verified_count == 1
    assert result.pinned_count == 1


async def test_inventory_marks_invalid_verification_for_manual_review(tmp_path) -> None:
    settings, manifest = _package(tmp_path)
    BackupVerificationStore(settings.BACKUP_DIR).write_passed(
        manifest=manifest, manifest_sha256="f" * 64
    )

    result = await BackupInventoryService(
        settings,
        runner=FakeRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).inventory()

    item = result.entries[0]
    assert item.restore_verified == "no"
    assert item.verification_status == "invalid"
    assert item.retention_disposition == "manual_review"
    assert item.error_code == "BACKUP_VERIFICATION_IDENTITY_INVALID"


async def test_inventory_counts_noncanonical_root_entry_without_exposing_it(
    tmp_path,
) -> None:
    settings, _ = _package(tmp_path)
    (settings.BACKUP_DIR / "unexpected.txt").write_text("x", encoding="utf-8")

    result = await BackupInventoryService(
        settings,
        runner=FakeRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).inventory()

    assert result.status == "pass"
    assert result.unrecognized_entry_count == 1
    assert all("unexpected" not in item.model_dump_json() for item in result.entries)
