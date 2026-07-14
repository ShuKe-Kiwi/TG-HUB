"""Controlled PostgreSQL backup package creation for P6-Deploy-4B."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import shutil
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Mapping, Sequence

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings, load_settings
from app.deploy.backup_fs import (
    BackupFsError,
    atomic_write_bytes,
    backup_root_lock,
    ensure_private_directory,
    fsync_directory,
    read_regular_exact,
    stream_sha256,
)
from app.deploy.backup_models import (
    BackupExclusions,
    BackupManifest,
    BackupPreflightResult,
    BackupRunResult,
    DatabaseBackupManifest,
    PgConnectionSpec,
    WatchlistBackupManifest,
    require_backup_version_compatibility,
)
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.watchlist_service import watchlist_revision

MAX_TOOL_OUTPUT_BYTES = 1024 * 1024
MAX_WATCHLIST_BYTES = 1024 * 1024
TOOL_TIMEOUT_SECONDS = 120.0
MIN_SPACE_RESERVE_BYTES = 256 * 1024 * 1024
VERSION_PATTERN = re.compile(r"(?:PostgreSQL\)?\s+)([0-9]+)(?:\.[0-9]+)*")
BACKUP_ID_TIME_FORMAT = "%Y%m%dT%H%M%S.%fZ"


class BackupServiceError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class ToolResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class SnapshotMetadata:
    snapshot_id: str
    source_server_version: str
    source_server_major: int
    database_size_bytes: int
    alembic_revision: str


class PgToolRunner:
    """Own one bounded subprocess until all pipes and process state are settled."""

    def __init__(
        self,
        *,
        timeout_seconds: float = TOOL_TIMEOUT_SECONDS,
        output_limit_bytes: int = MAX_TOOL_OUTPUT_BYTES,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes

    async def run(
        self,
        argv: Sequence[str | Path],
        *,
        env: Mapping[str, str],
        cwd: Path | None = None,
    ) -> ToolResult:
        process = await asyncio.create_subprocess_exec(
            *(str(value) for value in argv),
            cwd=cwd,
            env=dict(env),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        communicate = asyncio.create_task(self._communicate_bounded(process))
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    asyncio.shield(communicate), self.timeout_seconds
                )
            except asyncio.TimeoutError as exc:
                await self._terminate(process, communicate)
                raise BackupServiceError("PG_TOOL_TIMEOUT") from exc
            except asyncio.CancelledError:
                await self._terminate(process, communicate)
                raise
            except BackupServiceError:
                await self._terminate(process, communicate)
                raise
        finally:
            if process.returncode is None:
                await self._terminate(process, communicate)
        return ToolResult(process.returncode or 0, stdout, stderr)

    async def _communicate_bounded(
        self, process: asyncio.subprocess.Process
    ) -> tuple[bytes, bytes]:
        async def read(stream: asyncio.StreamReader | None) -> bytes:
            if stream is None:
                return b""
            chunks: list[bytes] = []
            total = 0
            while chunk := await stream.read(64 * 1024):
                total += len(chunk)
                if total > self.output_limit_bytes:
                    raise BackupServiceError("PG_TOOL_OUTPUT_LIMIT_EXCEEDED")
                chunks.append(chunk)
            return b"".join(chunks)

        stdout_task = asyncio.create_task(read(process.stdout))
        stderr_task = asyncio.create_task(read(process.stderr))
        try:
            stdout, stderr, _ = await asyncio.gather(
                stdout_task,
                stderr_task,
                process.wait(),
            )
            if len(stdout) + len(stderr) > self.output_limit_bytes:
                raise BackupServiceError("PG_TOOL_OUTPUT_LIMIT_EXCEEDED")
            return stdout, stderr
        finally:
            for task in (stdout_task, stderr_task):
                if not task.done():
                    task.cancel()

    async def _terminate(
        self,
        process: asyncio.subprocess.Process,
        communicate: asyncio.Task[tuple[bytes, bytes]],
    ) -> None:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(asyncio.shield(communicate), 2.0)
        except BackupServiceError:
            await process.wait()
        except asyncio.TimeoutError:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(communicate), 2.0)
            except BackupServiceError:
                await process.wait()
            except asyncio.TimeoutError as exc:
                raise BackupServiceError(
                    "BACKUP_SUBPROCESS_CLEANUP_FAILED"
                ) from exc


def parse_pg_connection_spec(database_url: str) -> PgConnectionSpec:
    try:
        url = make_url(database_url)
    except Exception as exc:
        raise BackupServiceError("DATABASE_URL_UNSUPPORTED") from exc
    if not url.drivername.startswith("postgresql+") or url.query:
        raise BackupServiceError("DATABASE_URL_UNSUPPORTED")
    if (
        not url.host
        or url.host.startswith("/")
        or "%" in url.host
        or not url.username
        or not url.database
    ):
        raise BackupServiceError("DATABASE_URL_UNSUPPORTED")
    try:
        return PgConnectionSpec(
            host=url.host,
            port=url.port,
            user=url.username,
            database=url.database,
            password=url.password,
        )
    except Exception as exc:
        raise BackupServiceError("DATABASE_URL_UNSUPPORTED") from exc


def build_libpq_env(
    spec: PgConnectionSpec,
    *,
    parent_env: Mapping[str, str],
    path: str,
) -> dict[str, str]:
    env = {"PATH": path}
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"):
        value = parent_env.get(key)
        if value:
            env[key] = value
    env.update(
        {
            "PGHOST": spec.host,
            "PGUSER": spec.user,
            "PGDATABASE": spec.database,
        }
    )
    if spec.port is not None:
        env["PGPORT"] = str(spec.port)
    if spec.password is not None:
        env["PGPASSWORD"] = spec.password
    return env


def parse_pg_major(output: bytes) -> int:
    try:
        text_value = output.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BackupServiceError("PG_DUMP_VERSION_UNSUPPORTED") from exc
    match = VERSION_PATTERN.search(text_value)
    if match is None:
        raise BackupServiceError("PG_DUMP_VERSION_UNSUPPORTED")
    return int(match.group(1))


def required_free_bytes(database_size_bytes: int) -> int:
    if database_size_bytes < 0:
        raise ValueError("database size must not be negative")
    return max(
        database_size_bytes * 2,
        database_size_bytes + MIN_SPACE_RESERVE_BYTES,
    )


def required_catalog_objects_present(catalog: bytes) -> bool:
    required_tables = {
        "alembic_version",
        "channels",
        "raw_messages",
        "works",
        "resources",
        "resource_links",
        "resource_sources",
    }
    required_data = required_tables - {"alembic_version"}
    tables: set[str] = set()
    table_data: set[str] = set()
    for raw_line in catalog.decode("utf-8", errors="replace").splitlines():
        if ";" not in raw_line or raw_line.lstrip().startswith(";"):
            continue
        fields = raw_line.split(";", 1)[1].strip().split()
        if len(fields) < 6:
            continue
        if fields[2] == "TABLE" and fields[3] == "DATA" and len(fields) >= 7:
            if fields[4] == "public":
                table_data.add(fields[5])
        elif fields[2] == "TABLE" and fields[3] == "public":
            tables.add(fields[4])
    return required_tables <= tables and required_data <= table_data


@asynccontextmanager
async def exported_snapshot(
    database_url: str,
) -> AsyncIterator[SnapshotMetadata]:
    engine = create_async_engine(database_url, pool_pre_ping=True)
    connection = None
    transaction = None
    try:
        connection = await engine.connect()
        transaction = await connection.begin()
        await connection.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        )
        version_num = str(
            (await connection.execute(text("SHOW server_version_num"))).scalar_one()
        )
        server_version = str(
            (await connection.execute(text("SHOW server_version"))).scalar_one()
        )
        size = int(
            (
                await connection.execute(
                    text("SELECT pg_database_size(current_database())")
                )
            ).scalar_one()
        )
        revision = str(
            (
                await connection.execute(
                    text("SELECT version_num FROM alembic_version")
                )
            ).scalar_one()
        )
        snapshot_id = str(
            (await connection.execute(text("SELECT pg_export_snapshot()"))).scalar_one()
        )
        metadata = SnapshotMetadata(
            snapshot_id=snapshot_id,
            source_server_version=server_version,
            source_server_major=int(version_num) // 10000,
            database_size_bytes=size,
            alembic_revision=revision,
        )
    except asyncio.CancelledError:
        if transaction is not None and transaction.is_active:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        await engine.dispose()
        raise
    except Exception as exc:
        if transaction is not None and transaction.is_active:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        await engine.dispose()
        raise BackupServiceError("DATABASE_SNAPSHOT_EXPORT_FAILED") from exc
    try:
        yield metadata
    finally:
        if transaction is not None and transaction.is_active:
            await transaction.rollback()
        if connection is not None:
            await connection.close()
        await engine.dispose()


class BackupService:
    def __init__(
        self,
        settings: Settings,
        *,
        repository_root: Path | None = None,
        runner: PgToolRunner | None = None,
        allow_test_paths: bool = False,
    ) -> None:
        self.settings = settings
        self.repository_root = repository_root or Path(__file__).parents[3]
        self.runner = runner or PgToolRunner()
        self.allow_test_paths = allow_test_paths

    async def preflight(self) -> BackupPreflightResult:
        config_ok = self.settings.APP_ENV == "production"
        paths_ok = _private_path(self.settings.BACKUP_DIR) and _private_path(
            self.settings.WATCHLIST_PATH
        )
        lock_path = self.repository_root / "backend" / "uv.lock"
        lock_ok = lock_path.is_file()
        git_clean = await _git_clean(self.repository_root)
        error = None
        if not config_ok or not paths_ok:
            error = "BACKUP_PATH_INVALID"
        elif not lock_ok:
            error = "BACKUP_MANIFEST_INVALID"
        elif not git_clean:
            error = "GIT_WORKTREE_DIRTY"
        return BackupPreflightResult(
            status="pass" if error is None else "fail",
            config_valid="yes" if config_ok else "no",
            paths_valid="yes" if paths_ok else "no",
            git_clean="yes" if git_clean else "no",
            dependency_lock_valid="yes" if lock_ok else "no",
            error_code=error,
        )

    async def create(self) -> BackupRunResult:
        if self.settings.APP_ENV != "production" or (
            not self.allow_test_paths
            and (
                not _private_path(self.settings.BACKUP_DIR)
                or not _private_path(self.settings.WATCHLIST_PATH)
            )
        ):
            return _failed_result("BACKUP_PATH_INVALID")
        backup_root = self.settings.BACKUP_DIR.expanduser()
        backup_id = _new_backup_id()
        temp_package = backup_root / ".tmp" / backup_id
        final_package = backup_root / backup_id
        try:
            ensure_private_directory(backup_root)
            ensure_private_directory(backup_root / ".tmp")
            with backup_root_lock(backup_root, mode="exclusive"):
                return await self._create_locked(
                    backup_id, temp_package, final_package
                )
        except asyncio.CancelledError:
            _cleanup_owned_temp(temp_package)
            raise
        except (BackupServiceError, BackupFsError) as exc:
            cleanup_failed = not _cleanup_owned_temp(temp_package)
            code = "BACKUP_CLEANUP_REQUIRED" if cleanup_failed else exc.error_code
            return _failed_result(code, temp_only=cleanup_failed)
        except OSError as exc:
            cleanup_failed = not _cleanup_owned_temp(temp_package)
            code = (
                "BACKUP_SPACE_INSUFFICIENT"
                if exc.errno == errno.ENOSPC
                else "BACKUP_PATH_INVALID"
            )
            if cleanup_failed:
                code = "BACKUP_CLEANUP_REQUIRED"
            return _failed_result(code, temp_only=cleanup_failed)

    async def _create_locked(
        self, backup_id: str, temp_package: Path, final_package: Path
    ) -> BackupRunResult:
        if not await _git_clean(self.repository_root):
            raise BackupServiceError("GIT_WORKTREE_DIRTY")
        git_commit = _git_output(self.repository_root, ["rev-parse", "HEAD"])
        pg_dump = _resolve_tool("pg_dump")
        pg_restore = _resolve_tool("pg_restore")
        version_env = _version_env(pg_dump.parent)
        dump_version = await self.runner.run(
            [pg_dump, "--version"], env=version_env
        )
        restore_version = await self.runner.run(
            [pg_restore, "--version"], env=version_env
        )
        if dump_version.returncode or restore_version.returncode:
            raise BackupServiceError("BACKUP_TOOL_MISSING")
        pg_dump_major = parse_pg_major(dump_version.stdout)
        pg_restore_major = parse_pg_major(restore_version.stdout)
        spec = parse_pg_connection_spec(self.settings.DATABASE_URL)
        libpq_env = build_libpq_env(
            spec,
            parent_env=os.environ,
            path=str(pg_dump.parent),
        )
        expected_head = _expected_alembic_head(self.repository_root / "backend")
        async with exported_snapshot(self.settings.DATABASE_URL) as snapshot:
            if snapshot.alembic_revision != expected_head:
                raise BackupServiceError("MIGRATION_NOT_AT_HEAD")
            require_backup_version_compatibility(
                source_server_major=snapshot.source_server_major,
                pg_dump_major=pg_dump_major,
            )
            if pg_restore_major != pg_dump_major:
                raise BackupServiceError("PG_RESTORE_VERSION_UNSUPPORTED")
            if shutil.disk_usage(backup_root := temp_package.parents[1]).free < (
                required_free_bytes(snapshot.database_size_bytes)
            ):
                raise BackupServiceError("BACKUP_SPACE_INSUFFICIENT")
            temp_package.mkdir(mode=0o700)
            dump_temp = temp_package / "database.dump.tmp"
            dump_result = await self.runner.run(
                [
                    pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    f"--snapshot={snapshot.snapshot_id}",
                    f"--file={dump_temp}",
                ],
                env=libpq_env,
            )
            if dump_result.returncode:
                raise BackupServiceError("DATABASE_DUMP_FAILED")
            os.chmod(dump_temp, 0o600)
            catalog = await self.runner.run(
                [pg_restore, "--list", dump_temp], env=version_env
            )
            if catalog.returncode or not required_catalog_objects_present(catalog.stdout):
                raise BackupServiceError("DATABASE_DUMP_INVALID")
            dump_path = temp_package / "database.dump"
            os.replace(dump_temp, dump_path)
            fsync_directory(temp_package)
            watchlist_bytes, config = _snapshot_watchlist(
                self.settings.WATCHLIST_PATH.expanduser()
            )
            atomic_write_bytes(
                temp_package / "watchlist.json",
                watchlist_bytes,
                token=backup_id,
            )
            dump_hash, dump_size = stream_sha256(dump_path)
            watch_hash, watch_size = stream_sha256(temp_package / "watchlist.json")
            if (
                not await _git_clean(self.repository_root)
                or _git_output(self.repository_root, ["rev-parse", "HEAD"])
                != git_commit
            ):
                raise BackupServiceError("GIT_WORKTREE_DIRTY")
            try:
                manifest = _manifest(
                    backup_id=backup_id,
                    repository_root=self.repository_root,
                    snapshot=snapshot,
                    pg_dump_version=dump_version.stdout.decode("ascii").strip(),
                    pg_dump_major=pg_dump_major,
                    dump_hash=dump_hash,
                    dump_size=dump_size,
                    watch_hash=watch_hash,
                    watch_size=watch_size,
                    watch_revision=watchlist_revision(config),
                    git_commit=git_commit,
                    config_schema_version=self.settings.CONFIG_SCHEMA_VERSION,
                )
            except BackupServiceError:
                raise
            except Exception as exc:
                raise BackupServiceError("BACKUP_MANIFEST_INVALID") from exc
            encoded = json.dumps(
                manifest.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8") + b"\n"
            atomic_write_bytes(
                temp_package / "manifest.json", encoded, token=backup_id
            )
            fsync_directory(temp_package)
            os.replace(temp_package, final_package)
            try:
                fsync_directory(backup_root)
            except BackupFsError:
                return _uncertain_result(backup_id, dump_size + watch_size + len(encoded))
        return BackupRunResult(
            status="pass",
            backup_id=backup_id,
            package_state="final_committed",
            manifest_valid="yes",
            database_dump_valid="yes",
            watchlist_snapshot_valid="yes",
            backup_bytes=dump_size + watch_size + len(encoded),
        )


def _snapshot_watchlist(path: Path) -> tuple[bytes, WatchlistConfig]:
    try:
        payload = read_regular_exact(path, max_bytes=MAX_WATCHLIST_BYTES)
        return payload, WatchlistConfig.model_validate_json(payload)
    except Exception as exc:
        if isinstance(exc, BackupFsError) and exc.error_code == (
            "BACKUP_PACKAGE_CHANGED_DURING_VERIFY"
        ):
            raise BackupServiceError("WATCHLIST_CHANGED_DURING_BACKUP") from exc
        raise BackupServiceError("WATCHLIST_UNREADABLE") from exc


def _manifest(
    *,
    backup_id: str,
    repository_root: Path,
    snapshot: SnapshotMetadata,
    pg_dump_version: str,
    pg_dump_major: int,
    dump_hash: str,
    dump_size: int,
    watch_hash: str,
    watch_size: int,
    watch_revision: str,
    git_commit: str,
    config_schema_version: int,
) -> BackupManifest:
    lock_hash, _ = stream_sha256(repository_root / "backend" / "uv.lock")
    return BackupManifest(
        backup_id=backup_id,
        created_at_utc=datetime.now(timezone.utc),
        app_git_commit=git_commit,
        git_worktree_clean=True,
        python_version=(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        dependency_lock_sha256=lock_hash,
        alembic_revision=snapshot.alembic_revision,
        config_schema_version=config_schema_version,
        database=DatabaseBackupManifest(
            sha256=dump_hash,
            size_bytes=dump_size,
            source_server_version=snapshot.source_server_version,
            source_server_major=snapshot.source_server_major,
            pg_dump_version=pg_dump_version,
            pg_dump_major=pg_dump_major,
        ),
        watchlist=WatchlistBackupManifest(
            sha256=watch_hash,
            size_bytes=watch_size,
            revision=watch_revision,
        ),
        exclusions=BackupExclusions(
            production_env="secret_material_excluded",
            telethon_session="authentication_session_excluded",
            logs="operational_data_excluded",
            runtime_state="ephemeral_data_excluded",
        ),
    )


def _new_backup_id() -> str:
    import secrets

    return f"{datetime.now(timezone.utc).strftime(BACKUP_ID_TIME_FORMAT)}-{secrets.token_hex(16)}"


def _version_env(tool_dir: Path) -> dict[str, str]:
    env = {"PATH": str(tool_dir)}
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"):
        if value := os.environ.get(key):
            env[key] = value
    return env


def _resolve_tool(name: str) -> Path:
    resolved = shutil.which(name)
    if resolved is None:
        raise BackupServiceError("BACKUP_TOOL_MISSING")
    return Path(resolved).resolve()


def _expected_alembic_head(backend_root: Path) -> str:
    config = Config(str(backend_root / "alembic.ini"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise BackupServiceError("MIGRATION_NOT_AT_HEAD")
    return heads[0]


async def _git_clean(repository_root: Path) -> bool:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=normal",
            cwd=repository_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), 5.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return False
    return process.returncode == 0 and not stdout


def _git_output(repository_root: Path, args: list[str]) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repository_root,
            check=False,
            capture_output=True,
            timeout=5,
        )
        value = result.stdout.decode("ascii", errors="strict").strip()
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        raise BackupServiceError("GIT_WORKTREE_DIRTY") from exc
    if result.returncode or not value:
        raise BackupServiceError("GIT_WORKTREE_DIRTY")
    return value


def _private_path(path: Path) -> bool:
    expanded = path.expanduser().resolve(strict=False)
    private_root = (Path.home() / ".tg-hub").resolve(strict=False)
    try:
        expanded.relative_to(private_root)
    except ValueError:
        return False
    return True


def _cleanup_owned_temp(path: Path) -> bool:
    if path.parent.name != ".tmp" or not path.name.startswith("20"):
        return False
    try:
        shutil.rmtree(path, ignore_errors=False) if path.exists() else None
        return True
    except OSError:
        return False


def _failed_result(error_code: str, *, temp_only: bool = False) -> BackupRunResult:
    return BackupRunResult(
        status="fail",
        package_state="temp_only" if temp_only else "not_created",
        manifest_valid="no",
        database_dump_valid="no",
        watchlist_snapshot_valid="no",
        error_code=error_code,
    )


def _uncertain_result(backup_id: str, size: int) -> BackupRunResult:
    return BackupRunResult(
        status="fail",
        backup_id=backup_id,
        package_state="commit_uncertain",
        manifest_valid="yes",
        database_dump_valid="yes",
        watchlist_snapshot_valid="yes",
        backup_bytes=size,
        error_code="BACKUP_COMMIT_UNCERTAIN",
    )


async def _main() -> int:
    settings = load_settings()
    service = BackupService(settings)
    if "--preflight" in sys.argv:
        result = await service.preflight()
    elif len(sys.argv) == 2 and sys.argv[1] == "create":
        result = await service.create()
    else:
        print("usage: python -m app.deploy.backup_service --preflight|create")
        return 2
    print(result.model_dump_json())
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
