from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from app.deploy.backup_models import (
    BackupExclusions,
    BackupManifest,
    DatabaseBackupManifest,
    WatchlistBackupManifest,
)
from app.deploy.backup_service import ToolResult
from app.deploy.backup_verify import BackupPackageValidator
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.watchlist_service import watchlist_revision

BACKUP_ID = "20260714T120000.123456Z-0123456789abcdef0123456789abcdef"


def _catalog() -> bytes:
    tables = [
        "alembic_version",
        "channels",
        "raw_messages",
        "works",
        "resources",
        "resource_links",
        "resource_sources",
    ]
    lines: list[str] = []
    for index, table in enumerate(tables, 1):
        lines.append(f"{index}; 1259 1 TABLE public {table} owner")
        if table != "alembic_version":
            lines.append(f"{index + 20}; 0 1 TABLE DATA public {table} owner")
    return ("\n".join(lines) + "\n").encode()


class FakeRunner:
    async def run(self, argv, *, env, cwd=None):
        values = [str(value) for value in argv]
        if "--version" in values:
            return ToolResult(0, b"pg_restore (PostgreSQL) 16.4\n", b"")
        return ToolResult(0, _catalog(), b"")


def _settings(tmp_path: Path) -> Settings:
    return Settings(BACKUP_DIR=tmp_path / "backups")


def _package(tmp_path: Path) -> tuple[Settings, Path]:
    settings = _settings(tmp_path)
    package = settings.BACKUP_DIR / BACKUP_ID
    package.mkdir(parents=True, mode=0o700)
    settings.BACKUP_DIR.chmod(0o700)
    dump = b"PGDMP-fake"
    watch = json.dumps(
        {
            "source_channels": [{"ref": "https://t.me/example", "enabled": True}],
            "watch_titles": [{"title": "百花杀", "enabled": True, "aliases": []}],
        },
        ensure_ascii=False,
    ).encode()
    (package / "database.dump").write_bytes(dump)
    (package / "watchlist.json").write_bytes(watch)
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
    (package / "manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    for path in package.iterdir():
        path.chmod(0o600)
    return settings, package


async def test_validator_rejects_invalid_backup_id_without_path_access(
    tmp_path: Path,
) -> None:
    result = await BackupPackageValidator(
        _settings(tmp_path),
        runner=FakeRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).validate("../escape")

    assert result.status == "fail"
    assert result.error_code == "BACKUP_MANIFEST_INVALID"
    assert result.backup_id is None


class MissingCatalogRunner(FakeRunner):
    async def run(self, argv, *, env, cwd=None):
        values = [str(value) for value in argv]
        if "--list" in values:
            return ToolResult(0, b"1; 1259 1 TABLE public unrelated owner\n", b"")
        return await super().run(argv, env=env, cwd=cwd)


def test_validator_module_does_not_expose_arbitrary_path_entry() -> None:
    assert not hasattr(BackupPackageValidator, "validate_path")


async def test_validator_accepts_complete_final_package(tmp_path: Path) -> None:
    settings, _ = _package(tmp_path)

    result = await BackupPackageValidator(
        settings,
        runner=FakeRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).validate(BACKUP_ID)

    assert result.status == "pass"
    assert result.required_catalog_objects_present == "yes"


async def test_validator_rejects_checksum_change(tmp_path: Path) -> None:
    settings, package = _package(tmp_path)
    (package / "database.dump").write_bytes(b"changed")

    result = await BackupPackageValidator(
        settings,
        runner=FakeRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).validate(BACKUP_ID)

    assert result.status == "fail"
    assert result.error_code == "BACKUP_CHECKSUM_MISMATCH"


async def test_validator_rejects_dump_without_core_catalog(tmp_path: Path) -> None:
    settings, _ = _package(tmp_path)

    result = await BackupPackageValidator(
        settings,
        runner=MissingCatalogRunner(),
        pg_restore_path=Path("/tools/pg_restore"),
    ).validate(BACKUP_ID)

    assert result.status == "fail"
    assert result.error_code == "DATABASE_DUMP_INVALID"
