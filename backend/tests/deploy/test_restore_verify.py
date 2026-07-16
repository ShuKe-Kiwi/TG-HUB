from __future__ import annotations

import asyncio
import getpass
import hashlib
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.deploy.backup_models import (
    BackupExclusions,
    BackupManifest,
    BackupValidationResult,
    DatabaseBackupManifest,
    WatchlistBackupManifest,
)
from app.deploy.backup_fs import stream_sha256
from app.deploy.backup_service import (
    BackupServiceError,
    PgToolRunner,
    ToolResult,
    build_libpq_env,
    parse_pg_connection_spec,
)
from app.deploy.restore_database import (
    RestoreDatabaseError,
    TargetState,
    VerificationSummary,
    _expected_columns,
    _expected_constraints,
    _pg_char,
    database_url_for,
)
from app.deploy.restore_verify import RestoreVerificationService
from app.deploy.restore_recovery import RestoreRecoveryError, RestoreRecoveryStore
from app.deploy.backup_verification import BackupVerificationError


BACKUP_ID = "20260714T100055.207160Z-49502732533cd7470f883488ed0a6131"


class FakeValidator:
    async def validate_locked(self, backup_id: str) -> BackupValidationResult:
        return BackupValidationResult(
            status="pass", backup_id=backup_id, manifest_valid="yes",
            database_dump_valid="yes", watchlist_snapshot_valid="yes",
            required_catalog_objects_present="yes",
        )


class FakeRunner:
    def __init__(self, *, restore_returncode: int = 0) -> None:
        self.restore_returncode = restore_returncode
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    async def run(self, argv, *, env, cwd=None) -> ToolResult:
        values = [str(value) for value in argv]
        self.calls.append((values, dict(env)))
        if "--version" in values:
            return ToolResult(0, b"pg_restore (PostgreSQL) 16.4\n", b"")
        return ToolResult(self.restore_returncode, b"", b"failed")


class AbaMutationRunner(FakeRunner):
    def __init__(self, source: Path) -> None:
        super().__init__()
        self.source = source
        self.restored_payload: bytes | None = None

    async def run(self, argv, *, env, cwd=None) -> ToolResult:
        values = [str(value) for value in argv]
        if "--version" in values:
            return await super().run(argv, env=env, cwd=cwd)
        self.calls.append((values, dict(env)))
        original = self.source.read_bytes()
        self.source.write_bytes(b"temporary-aba-content")
        self.restored_payload = Path(values[-1]).read_bytes()
        self.source.write_bytes(original)
        return ToolResult(0, b"", b"")


class CancellingRunner(FakeRunner):
    async def run(self, argv, *, env, cwd=None) -> ToolResult:
        values = [str(value) for value in argv]
        if "--version" in values:
            return await super().run(argv, env=env, cwd=cwd)
        raise asyncio.CancelledError


class CapturingPgToolRunner(PgToolRunner):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.results: list[ToolResult] = []

    async def run(self, *args, **kwargs) -> ToolResult:
        result = await super().run(*args, **kwargs)
        self.results.append(result)
        return result


class FakeAdapter:
    def __init__(self) -> None:
        self.exists = False
        self.comment: str | None = None
        self.closed = False
        self.drop_calls = 0

    async def server_major(self) -> int:
        return 16

    async def create_target(self, target: str) -> None:
        self.exists = True

    async def commit_identity(self, target: str, token: str) -> None:
        self.comment = f"tg-hub-restore-verify:{token}"

    async def inspect_target(self, target: str) -> TargetState:
        return TargetState(self.exists, self.exists, self.comment, 0, 0)

    async def current_database_is(self, target: str) -> bool:
        return self.exists

    async def drop_target(self, target: str) -> None:
        self.drop_calls += 1
        self.exists = False

    async def aclose(self) -> None:
        self.closed = True


class CreateUncertainAdapter(FakeAdapter):
    async def create_target(self, target: str) -> None:
        self.exists = True
        raise RestoreDatabaseError("RESTORE_CREATE_FAILED")


class DeleteFailingStore(RestoreRecoveryStore):
    def delete(self, opaque_id: str) -> None:
        raise RestoreRecoveryError("RESTORE_RECOVERY_WRITE_FAILED")


class MutatingDeleteStore(RestoreRecoveryStore):
    def __init__(self, root: Path, dump_path: Path) -> None:
        super().__init__(root)
        self.dump_path = dump_path

    def delete(self, opaque_id: str) -> None:
        super().delete(opaque_id)
        self.dump_path.write_bytes(b"changed-after-restore")


class FakeVerifier:
    async def verify(self, target: str, *, expected_revision: str):
        return VerificationSummary(True, True, True)


class FakeVerificationStore:
    def __init__(self) -> None:
        self.writes = []

    def write_passed(self, **values):
        self.writes.append(values)


class FailingVerificationStore(FakeVerificationStore):
    def write_passed(self, **values):
        raise BackupVerificationError("BACKUP_VERIFICATION_WRITE_FAILED")


class FakeInventoryService:
    async def target_locked(self, root, backup_id):
        return SimpleNamespace(
            restore_verified="yes",
            verification_status="valid",
            verification_version=1,
            retention_disposition="keep",
        )


def _service(tmp_path: Path, *, restore_returncode: int = 0):
    backup_root = tmp_path / "backups"
    backup_root.mkdir(mode=0o700)
    package = backup_root / BACKUP_ID
    package.mkdir(mode=0o700)
    dump = b"fake"
    watchlist = b"{}"
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
            sha256=hashlib.sha256(watchlist).hexdigest(),
            size_bytes=len(watchlist),
            revision="revision",
        ),
        exclusions=BackupExclusions(
            production_env="secret_material_excluded",
            telethon_session="authentication_session_excluded",
            logs="operational_data_excluded",
            runtime_state="ephemeral_data_excluded",
        ),
    )
    (package / "database.dump").write_bytes(dump)
    (package / "watchlist.json").write_bytes(watchlist)
    (package / "manifest.json").write_bytes(
        manifest.model_dump_json().encode("utf-8")
    )
    for path in package.iterdir():
        os.chmod(path, 0o600)
    settings = Settings(
        DATABASE_URL="postgresql+asyncpg://user:secret@127.0.0.1:5432/tg_hub",
        BACKUP_DIR=backup_root,
        RESTORE_VERIFY_TIMEOUT_SECONDS=60,
    )
    runner = FakeRunner(restore_returncode=restore_returncode)
    adapter = FakeAdapter()
    verification_store = FakeVerificationStore()
    service = RestoreVerificationService(
        settings,
        repository_root=tmp_path,
        validator=FakeValidator(),
        runner=runner,
        adapter_factory=lambda *_: adapter,
        verifier_factory=lambda *_: FakeVerifier(),
        verification_store=verification_store,
        inventory_service=FakeInventoryService(),
        pg_restore_path=Path("/tools/pg_restore"),
    )
    service._expected_head = lambda: "head"
    return service, runner, adapter


async def test_run_uses_env_target_and_completes_fake_lifecycle(tmp_path) -> None:
    service, runner, adapter = _service(tmp_path)

    result = await service.run(BACKUP_ID)

    assert result.status == "pass", result.error_code
    restore_argv, restore_env = runner.calls[-1]
    dbname_args = [value for value in restore_argv if value.startswith("--dbname=")]
    assert dbname_args == [f"--dbname={restore_env['PGDATABASE']}"]
    assert "-d" not in restore_argv
    assert "--dbname=tg_hub" not in restore_argv
    assert all("://" not in value for value in restore_argv)
    assert sum(restore_env["PGDATABASE"] in value for value in restore_argv) == 1
    assert restore_env["PGDATABASE"].startswith("tg_hub_restore_verify_")
    assert adapter.drop_calls == 1
    assert adapter.closed is True
    assert len(service.verification_store.writes) == 1
    assert list(service.store.root.glob("*.json")) == []
    snapshot_path = Path(restore_argv[-1])
    assert snapshot_path.parent == service.snapshot_root
    assert snapshot_path != service.settings.BACKUP_DIR / BACKUP_ID / "database.dump"
    assert snapshot_path.exists() is False


async def test_restore_uses_private_snapshot_across_source_aba_mutation(
    tmp_path,
) -> None:
    service, _, _ = _service(tmp_path)
    source = service.settings.BACKUP_DIR / BACKUP_ID / "database.dump"
    runner = AbaMutationRunner(source)
    service.runner = runner

    result = await service.run(BACKUP_ID)

    assert result.status == "pass", result.error_code
    assert runner.restored_payload == b"fake"
    assert source.read_bytes() == b"fake"
    assert list(service.snapshot_root.glob("*.dump")) == []


async def test_cancelled_restore_retains_owned_snapshot_until_cleanup(
    tmp_path,
) -> None:
    service, _, adapter = _service(tmp_path)
    service.runner = CancellingRunner()

    def fail_immediate_cleanup(path: Path) -> None:
        raise BackupServiceError("RESTORE_SNAPSHOT_CLEANUP_FAILED")

    service._remove_restore_snapshot = fail_immediate_cleanup

    with pytest.raises(asyncio.CancelledError):
        await service.run(BACKUP_ID)

    records = list(service.store.root.glob("*.json"))
    snapshots = list(service.snapshot_root.glob("*.dump"))
    assert len(records) == 1
    assert len(snapshots) == 1
    record = service.store.read(records[0].stem)
    assert record.phase == "restore_failed"
    assert snapshots[0].name == f"{record.opaque_id}.dump"

    cleanup = await service.cleanup(
        backup_id=BACKUP_ID,
        recovery_record=record.opaque_id,
    )

    assert cleanup.status == "pass", cleanup.error_code
    assert cleanup.target_dropped == "yes"
    assert cleanup.record_deleted == "yes"
    assert adapter.exists is False
    assert list(service.snapshot_root.glob("*.dump")) == []
    assert list(service.store.root.glob("*.json")) == []


async def test_restore_failure_retains_record_for_cleanup(tmp_path) -> None:
    service, _, adapter = _service(tmp_path, restore_returncode=1)

    result = await service.run(BACKUP_ID)

    assert result.status == "fail"
    assert result.error_code == "RESTORE_FAILED"
    assert result.cleanup_required == "yes"
    assert result.cleanup_handle is not None
    assert service.store.read(result.cleanup_handle).phase == "restore_failed"
    assert adapter.exists is True


async def test_cleanup_reloads_record_under_lock_and_drops_target(tmp_path) -> None:
    service, _, adapter = _service(tmp_path, restore_returncode=1)
    failed = await service.run(BACKUP_ID)

    result = await service.cleanup(
        backup_id=BACKUP_ID, recovery_record=failed.cleanup_handle
    )

    assert result.status == "pass", result.error_code
    assert result.target_dropped == "yes"
    assert result.record_deleted == "yes"
    assert adapter.exists is False


async def test_snapshot_cleanup_failure_precedes_drop_and_can_retry(
    tmp_path,
) -> None:
    service, _, adapter = _service(tmp_path, restore_returncode=1)
    failed = await service.run(BACKUP_ID)
    original_cleanup = service._cleanup_owned_snapshot
    attempts = 0

    def fail_once(opaque_id: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RestoreRecoveryError("RESTORE_SNAPSHOT_CLEANUP_FAILED")
        original_cleanup(opaque_id)

    service._cleanup_owned_snapshot = fail_once

    first = await service.cleanup(
        backup_id=BACKUP_ID,
        recovery_record=failed.cleanup_handle,
    )
    assert first.status == "fail"
    assert first.error_code == "RESTORE_SNAPSHOT_CLEANUP_FAILED"
    assert first.target_dropped == "no"
    assert first.record_deleted == "no"
    assert adapter.exists is True

    second = await service.cleanup(
        backup_id=BACKUP_ID,
        recovery_record=failed.cleanup_handle,
    )
    assert second.status == "pass", second.error_code
    assert second.target_dropped == "yes"
    assert second.record_deleted == "yes"
    assert adapter.exists is False


async def test_create_uncertainty_retains_create_started_cleanup_handle(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    adapter = CreateUncertainAdapter()
    service.adapter_factory = lambda *_: adapter

    result = await service.run(BACKUP_ID)

    assert result.status == "fail"
    assert result.error_code == "RESTORE_CREATE_FAILED"
    assert result.target_created == "no"
    assert result.cleanup_required == "yes"
    assert service.store.read(result.cleanup_handle).phase == "create_started"
    cleanup = await service.cleanup(
        backup_id=BACKUP_ID, recovery_record=result.cleanup_handle
    )
    assert cleanup.status == "pass"
    assert cleanup.target_dropped == "yes"


async def test_drop_success_record_delete_failure_reports_dropped_target(tmp_path) -> None:
    service, _, adapter = _service(tmp_path)
    service.store = DeleteFailingStore(service.store.root)

    result = await service.run(BACKUP_ID)

    assert result.status == "fail"
    assert result.error_code == "RESTORE_RECOVERY_WRITE_FAILED"
    assert result.target_dropped == "yes"
    assert result.cleanup_required == "yes"
    assert adapter.exists is False
    assert service.store.read(result.cleanup_handle).phase == "verification_passed"


async def test_sidecar_write_failure_preserves_completed_restore_facts(tmp_path) -> None:
    service, _, adapter = _service(tmp_path)
    service.verification_store = FailingVerificationStore()

    result = await service.run(BACKUP_ID)

    assert result.status == "fail"
    assert result.error_code == "BACKUP_VERIFICATION_WRITE_FAILED"
    assert result.target_created == "yes"
    assert result.restore_completed == "yes"
    assert result.schema_verified == "yes"
    assert result.constraints_verified == "yes"
    assert result.integrity_verified == "yes"
    assert result.target_dropped == "yes"
    assert result.cleanup_required == "no"
    assert adapter.exists is False
    assert list(service.store.root.glob("*.json")) == []


async def test_package_change_after_cleanup_refuses_sidecar(tmp_path) -> None:
    service, _, adapter = _service(tmp_path)
    service.store = MutatingDeleteStore(
        service.store.root,
        service.settings.BACKUP_DIR / BACKUP_ID / "database.dump",
    )

    result = await service.run(BACKUP_ID)

    assert result.status == "fail"
    assert result.error_code == "BACKUP_PACKAGE_CHANGED_DURING_VERIFY"
    assert result.target_dropped == "yes"
    assert result.cleanup_required == "no"
    assert adapter.exists is False
    assert service.verification_store.writes == []
    assert list(service.store.root.glob("*.json")) == []


def test_equivalent_package_directory_replacement_preserves_identity(
    tmp_path,
) -> None:
    service, _, _ = _service(tmp_path)
    root = service.settings.BACKUP_DIR
    package = root / BACKUP_ID
    replacement = root / f"{BACKUP_ID}.replacement"
    displaced = root / f"{BACKUP_ID}.displaced"
    initial = service._capture_package_identity(root, BACKUP_ID)
    shutil.copytree(package, replacement)
    package.rename(displaced)
    replacement.rename(package)

    final = service._capture_package_identity(root, BACKUP_ID)

    assert initial.package_directory_identity != final.package_directory_identity
    assert service._same_package_identity(initial, final) is True


def test_backup_root_identity_change_invalidates_package_identity(tmp_path) -> None:
    service, _, _ = _service(tmp_path)
    root = service.settings.BACKUP_DIR
    displaced = tmp_path / "backups.displaced"
    initial = service._capture_package_identity(root, BACKUP_ID)
    root.rename(displaced)
    shutil.copytree(displaced, root)

    final = service._capture_package_identity(root, BACKUP_ID)

    assert initial.root_identity != final.root_identity
    assert service._same_package_identity(initial, final) is False


@pytest.mark.skipif(
    os.environ.get("RUN_RESTORE_VERIFY_INTEGRATION") != "1",
    reason="requires explicitly approved local PostgreSQL test fixture restore",
)
async def test_real_temporary_fixture_dump_restore_verify_and_drop(tmp_path) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database import Base
    from app.modules.channel import model as _channel_model  # noqa: F401
    from app.modules.rawmessage import model as _rawmessage_model  # noqa: F401
    from app.modules.resource import model as _resource_model  # noqa: F401

    user = getpass.getuser()
    database_url = f"postgresql+asyncpg://{user}@127.0.0.1:5432/tg_hub_test"
    pg_dump = Path(shutil.which("pg_dump") or "")
    pg_restore = Path(shutil.which("pg_restore") or "")
    assert pg_dump.is_file() and pg_restore.is_file()
    backup_root = tmp_path / "backups"
    package = backup_root / BACKUP_ID
    package.mkdir(mode=0o700, parents=True)
    os.chmod(backup_root, 0o700)
    dump_path = package / "database.dump"
    manifest_path = package / "manifest.json"
    watchlist_path = package / "watchlist.json"
    spec = parse_pg_connection_spec(database_url)
    runner = CapturingPgToolRunner(timeout_seconds=120)
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with engine.connect() as connection:
            revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            server_major = int(await connection.scalar(text("SHOW server_version_num"))) // 10000
        dump_result = await runner.run(
            [
                pg_dump, "--format=custom", "--no-owner", "--no-acl",
                f"--file={dump_path}",
            ],
            env=build_libpq_env(
                spec, parent_env=os.environ, path=str(pg_dump.parent)
            ),
        )
        assert dump_result.returncode == 0
        os.chmod(dump_path, 0o600)
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
    dump_sha256, dump_size = stream_sha256(dump_path)
    watchlist = b"{}"
    watchlist_path.write_bytes(watchlist)
    manifest = BackupManifest(
        backup_id=BACKUP_ID,
        created_at_utc=datetime.now(timezone.utc),
        app_git_commit="a" * 40,
        git_worktree_clean=True,
        python_version="3.11",
        dependency_lock_sha256="b" * 64,
        alembic_revision=str(revision),
        config_schema_version=1,
        database=DatabaseBackupManifest(
            sha256=dump_sha256,
            size_bytes=dump_size,
            source_server_version=f"{server_major}.fixture",
            source_server_major=server_major,
            pg_dump_version=f"pg_dump (PostgreSQL) {server_major}.fixture",
            pg_dump_major=server_major,
        ),
        watchlist=WatchlistBackupManifest(
            sha256=hashlib.sha256(watchlist).hexdigest(),
            size_bytes=len(watchlist),
            revision="fixture",
        ),
        exclusions=BackupExclusions(
            production_env="secret_material_excluded",
            telethon_session="authentication_session_excluded",
            logs="operational_data_excluded",
            runtime_state="ephemeral_data_excluded",
        ),
    )
    manifest_path.write_bytes(manifest.model_dump_json().encode("utf-8"))
    os.chmod(manifest_path, 0o600)
    os.chmod(watchlist_path, 0o600)
    settings = Settings(
        DATABASE_URL=database_url,
        BACKUP_DIR=backup_root,
        RESTORE_VERIFY_TIMEOUT_SECONDS=120,
    )
    service = RestoreVerificationService(
        settings,
        repository_root=tmp_path,
        validator=FakeValidator(),
        runner=runner,
        verification_store=FakeVerificationStore(),
        inventory_service=FakeInventoryService(),
        pg_restore_path=pg_restore,
    )
    service._expected_head = lambda: str(revision)

    result = await service.run(BACKUP_ID)

    diagnostic = runner.results[-1].stderr.decode("utf-8", errors="replace")
    if result.status == "fail" and result.cleanup_handle is not None:
        record = service.store.read(result.cleanup_handle)
        target_engine = create_async_engine(
            database_url_for(database_url, record.generated_target_name)
        )
        try:
            async with target_engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        """
                        SELECT c.relname, a.attname,
                               format_type(a.atttypid, a.atttypmod), a.attnotnull
                          FROM pg_attribute a
                          JOIN pg_class c ON c.oid = a.attrelid
                          JOIN pg_namespace n ON n.oid = c.relnamespace
                         WHERE n.nspname = 'public' AND c.relkind = 'r'
                           AND a.attnum > 0 AND NOT a.attisdropped
                        """
                    )
                )
                actual = {
                    (str(row[0]), str(row[1])): (
                        str(row[2]).lower(), bool(row[3])
                    )
                    for row in rows
                }
                constraint_rows = await connection.execute(
                    text(
                        """
                        SELECT c.contype, src.relname,
                               ARRAY(SELECT a.attname FROM unnest(c.conkey)
                                     WITH ORDINALITY k(attnum, ord)
                                     JOIN pg_attribute a ON a.attrelid=c.conrelid
                                      AND a.attnum=k.attnum ORDER BY k.ord),
                               dst.relname,
                               CASE WHEN c.contype='f' THEN ARRAY(
                                   SELECT a.attname FROM unnest(c.confkey)
                                   WITH ORDINALITY k(attnum, ord)
                                   JOIN pg_attribute a ON a.attrelid=c.confrelid
                                    AND a.attnum=k.attnum ORDER BY k.ord)
                               ELSE ARRAY[]::name[] END,
                               c.confdeltype, c.confupdtype, c.convalidated
                          FROM pg_constraint c
                          JOIN pg_class src ON src.oid=c.conrelid
                          JOIN pg_namespace n ON n.oid=src.relnamespace
                          LEFT JOIN pg_class dst ON dst.oid=c.confrelid
                         WHERE n.nspname='public' AND c.contype IN ('p','u','f')
                        """
                    )
                )
                actual_constraints = {
                    (
                            _pg_char(row[0]), str(row[1]), tuple(row[2]),
                        str(row[3]) if row[3] is not None else None,
                            tuple(row[4]), _pg_char(row[5]), _pg_char(row[6]),
                    )
                    for row in constraint_rows
                    if row[7]
                }
        finally:
            await target_engine.dispose()
        mismatches = {
            key: (expected, actual.get(key))
            for key, expected in _expected_columns().items()
            if actual.get(key) != expected
        }
        missing_constraints = _expected_constraints() - actual_constraints
        cleanup = await service.cleanup(
            backup_id=BACKUP_ID, recovery_record=result.cleanup_handle
        )
        assert cleanup.status == "pass", cleanup.error_code
        diagnostic = (
            f"{diagnostic}; column_mismatches={mismatches}; "
            f"missing_constraints={missing_constraints}; "
            f"actual_constraints={actual_constraints}"
        )
    assert result.status == "pass", f"{result.error_code}: {diagnostic}"
    assert result.target_dropped == "yes"
    assert result.cleanup_required == "no"
    assert list(service.store.root.glob("*.json")) == []
