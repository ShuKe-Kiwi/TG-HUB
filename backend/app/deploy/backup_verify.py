"""Read-only final backup package validation for P6-Deploy-4B."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings, load_settings
from app.deploy.backup_fs import (
    BackupFsError,
    backup_root_lock,
    read_regular_exact,
    stream_sha256,
    validate_backup_root,
)
from app.deploy.backup_models import (
    BackupManifest,
    BackupValidationResult,
    validate_backup_id,
)
from app.deploy.backup_service import (
    MAX_WATCHLIST_BYTES,
    PgToolRunner,
    BackupServiceError,
    parse_pg_major,
    required_catalog_objects_present,
)
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.watchlist_service import watchlist_revision

MAX_MANIFEST_BYTES = 256 * 1024
EXPECTED_FILES = {"database.dump", "watchlist.json", "manifest.json"}


class BackupPackageValidator:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: PgToolRunner | None = None,
        pg_restore_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.runner = runner or PgToolRunner()
        self.pg_restore_path = pg_restore_path

    async def validate(self, backup_id: str) -> BackupValidationResult:
        try:
            validate_backup_id(backup_id)
        except ValueError:
            return _failed(backup_id, "BACKUP_MANIFEST_INVALID")
        root = self.settings.BACKUP_DIR.expanduser()
        try:
            validate_backup_root(root)
            with backup_root_lock(root, mode="shared"):
                return await self._validate_locked(root / backup_id, backup_id)
        except (BackupFsError, BackupServiceError) as exc:
            return _failed(backup_id, exc.error_code)

    async def validate_locked(self, backup_id: str) -> BackupValidationResult:
        """Validate while the caller owns the backup-root shared lock."""
        try:
            validate_backup_id(backup_id)
            root = self.settings.BACKUP_DIR.expanduser()
            validate_backup_root(root)
            return await self._validate_locked(root / backup_id, backup_id)
        except ValueError:
            return _failed(backup_id, "BACKUP_MANIFEST_INVALID")
        except (BackupFsError, BackupServiceError) as exc:
            return _failed(backup_id, exc.error_code)

    async def _validate_locked(
        self, package: Path, backup_id: str
    ) -> BackupValidationResult:
        _validate_package_directory(package)
        names = {entry.name for entry in package.iterdir()}
        if names != EXPECTED_FILES:
            raise BackupServiceError("BACKUP_MANIFEST_INVALID")
        try:
            payload = json.loads(
                read_regular_exact(
                    package / "manifest.json", max_bytes=MAX_MANIFEST_BYTES
                )
            )
            manifest = BackupManifest.model_validate(payload)
        except (ValueError, TypeError, ValidationError, BackupFsError) as exc:
            raise BackupServiceError("BACKUP_MANIFEST_INVALID") from exc
        if manifest.backup_id != backup_id:
            raise BackupServiceError("BACKUP_MANIFEST_INVALID")
        dump_hash, dump_size = stream_sha256(package / "database.dump")
        watch_hash, watch_size = stream_sha256(package / "watchlist.json")
        if (
            dump_hash != manifest.database.sha256
            or dump_size != manifest.database.size_bytes
            or watch_hash != manifest.watchlist.sha256
            or watch_size != manifest.watchlist.size_bytes
        ):
            raise BackupServiceError("BACKUP_CHECKSUM_MISMATCH")
        try:
            watch_payload = read_regular_exact(
                package / "watchlist.json", max_bytes=MAX_WATCHLIST_BYTES
            )
            watchlist = WatchlistConfig.model_validate_json(watch_payload)
        except Exception as exc:
            raise BackupServiceError("BACKUP_MANIFEST_INVALID") from exc
        if watchlist_revision(watchlist) != manifest.watchlist.revision:
            raise BackupServiceError("BACKUP_CHECKSUM_MISMATCH")
        pg_restore = self.pg_restore_path or _resolve_restore()
        env = _version_env(pg_restore.parent)
        version = await self.runner.run([pg_restore, "--version"], env=env)
        if version.returncode or parse_pg_major(version.stdout) != (
            manifest.database.pg_dump_major
        ):
            raise BackupServiceError("PG_RESTORE_VERSION_UNSUPPORTED")
        catalog = await self.runner.run(
            [pg_restore, "--list", package / "database.dump"], env=env
        )
        catalog_ok = catalog.returncode == 0 and required_catalog_objects_present(
            catalog.stdout
        )
        if not catalog_ok:
            raise BackupServiceError("DATABASE_DUMP_INVALID")
        return BackupValidationResult(
            status="pass",
            backup_id=backup_id,
            manifest_valid="yes",
            database_dump_valid="yes",
            watchlist_snapshot_valid="yes",
            required_catalog_objects_present="yes",
        )


def _validate_package_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupServiceError("BACKUP_MANIFEST_INVALID") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise BackupServiceError("BACKUP_MANIFEST_INVALID")
    for entry in path.iterdir():
        info = entry.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise BackupServiceError("BACKUP_MANIFEST_INVALID")


def _resolve_restore() -> Path:
    resolved = shutil.which("pg_restore")
    if resolved is None:
        raise BackupServiceError("RESTORE_TOOL_MISSING")
    return Path(resolved).resolve()


def _version_env(tool_dir: Path) -> dict[str, str]:
    env = {"PATH": str(tool_dir)}
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"):
        if value := os.environ.get(key):
            env[key] = value
    return env


def _failed(backup_id: str, error_code: str) -> BackupValidationResult:
    safe_id: str | None = backup_id
    try:
        validate_backup_id(backup_id)
    except ValueError:
        safe_id = None
    return BackupValidationResult(
        status="fail",
        backup_id=safe_id,
        manifest_valid="no",
        database_dump_valid="no",
        watchlist_snapshot_valid="no",
        required_catalog_objects_present="no",
        error_code=error_code,
    )


async def _main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] != "--backup-id":
        print("usage: python -m app.deploy.backup_verify --backup-id <backup_id>")
        return 2
    result = await BackupPackageValidator(load_settings()).validate(sys.argv[2])
    print(result.model_dump_json())
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
