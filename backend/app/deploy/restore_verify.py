"""C1 isolated restore orchestration; real package execution remains separately gated."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import Settings, load_settings
from app.deploy.backup_fs import (
    BackupFsError,
    backup_root_lock,
    read_regular_exact,
    stream_sha256,
)
from app.deploy.backup_models import (
    BackupManifest,
    PgConnectionSpec,
    RestoreRecoveryRecord,
    RestoreCleanupResult,
    RestoreVerificationResult,
    generate_restore_identity,
    require_restore_version_compatibility,
    validate_backup_id,
)
from app.deploy.backup_service import (
    BackupServiceError,
    PgToolRunner,
    build_libpq_env,
    parse_pg_connection_spec,
    parse_pg_major,
)
from app.deploy.backup_verify import BackupPackageValidator, MAX_MANIFEST_BYTES
from app.deploy.backup_verification import (
    BackupVerificationError,
    BackupVerificationStore,
)
from app.deploy.restore_database import (
    IDENTITY_PREFIX,
    RestoreDatabaseAdapter,
    RestoreDatabaseError,
    RestoreDatabaseVerifier,
)
from app.deploy.restore_recovery import RestoreRecoveryError, RestoreRecoveryStore

MIN_RESTORE_TIMEOUT_SECONDS = 30
MAX_RESTORE_TIMEOUT_SECONDS = 6 * 60 * 60


class RestoreVerificationService:
    def __init__(
        self,
        settings: Settings,
        *,
        repository_root: Path | None = None,
        validator: BackupPackageValidator | None = None,
        runner: PgToolRunner | None = None,
        recovery_store: RestoreRecoveryStore | None = None,
        verification_store: BackupVerificationStore | None = None,
        adapter_factory: Callable[[str, PgConnectionSpec], RestoreDatabaseAdapter]
        | None = None,
        verifier_factory: Callable[[str], RestoreDatabaseVerifier] | None = None,
        pg_restore_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.repository_root = repository_root or Path(__file__).parents[3]
        self.timeout = settings.RESTORE_VERIFY_TIMEOUT_SECONDS
        self.runner = runner or PgToolRunner(timeout_seconds=float(self.timeout))
        self.validator = validator or BackupPackageValidator(
            settings, runner=self.runner, pg_restore_path=pg_restore_path
        )
        runtime_root = settings.BACKUP_DIR.expanduser().parent / "runtime"
        self.store = recovery_store or RestoreRecoveryStore(runtime_root)
        self.verification_store = verification_store or BackupVerificationStore(
            settings.BACKUP_DIR
        )
        self.adapter_factory = adapter_factory or (
            lambda url, spec: RestoreDatabaseAdapter(url, spec)
        )
        self.verifier_factory = verifier_factory or (
            lambda url: RestoreDatabaseVerifier(url)
        )
        self.pg_restore_path = pg_restore_path

    async def run(self, backup_id: str) -> RestoreVerificationResult:
        try:
            validate_backup_id(backup_id)
        except ValueError:
            return self._failed(backup_id, "BACKUP_MANIFEST_INVALID")
        if not MIN_RESTORE_TIMEOUT_SECONDS <= self.timeout <= MAX_RESTORE_TIMEOUT_SECONDS:
            return self._failed(backup_id, "RESTORE_TIMEOUT_INVALID")
        root = self.settings.BACKUP_DIR.expanduser()
        try:
            with backup_root_lock(root, mode="shared"):
                return await self._run_locked(root, backup_id)
        except asyncio.CancelledError:
            raise
        except (BackupFsError, BackupServiceError) as exc:
            return self._failed(backup_id, exc.error_code)
        except (RestoreDatabaseError, RestoreRecoveryError) as exc:
            return self._failed(backup_id, exc.error_code)

    async def cleanup(
        self, *, backup_id: str, recovery_record: str
    ) -> RestoreCleanupResult:
        validate_backup_id(backup_id)
        try:
            _, observed_backup_id = self.store.read_minimal(recovery_record)
            if observed_backup_id != backup_id:
                raise RestoreRecoveryError("RESTORE_CLEANUP_GUARD_FAILED")
            root = self.settings.BACKUP_DIR.expanduser()
            with backup_root_lock(root, mode="shared"):
                record = self.store.read(recovery_record)
                if record.backup_id != backup_id:
                    raise RestoreRecoveryError("RESTORE_CLEANUP_GUARD_FAILED")
                return await self._cleanup_locked(record)
        except asyncio.CancelledError:
            raise
        except (BackupFsError, RestoreRecoveryError) as exc:
            code = getattr(exc, "error_code", "RESTORE_CLEANUP_GUARD_FAILED")
            return RestoreCleanupResult(
                status="fail", backup_id=backup_id,
                cleanup_handle=recovery_record, target_dropped="no",
                record_deleted="no", error_code=code,
            )

    async def _cleanup_locked(
        self, record: RestoreRecoveryRecord
    ) -> RestoreCleanupResult:
        spec = parse_pg_connection_spec(self.settings.DATABASE_URL)
        adapter = self.adapter_factory(self.settings.DATABASE_URL, spec)
        dropped = False
        try:
            state = await adapter.inspect_target(record.generated_target_name)
            if not state.exists:
                if record.phase not in {
                    "planned", "create_started", "verification_passed", "drop_failed"
                }:
                    raise RestoreDatabaseError("RESTORE_CLEANUP_GUARD_FAILED")
            else:
                if not state.owner_matches:
                    raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")
                expected = f"{IDENTITY_PREFIX}{record.identity_token}"
                if record.phase == "planned":
                    raise RestoreDatabaseError("RESTORE_CLEANUP_GUARD_FAILED")
                if record.phase in {"create_started", "database_created"}:
                    if state.comment is not None:
                        raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")
                elif record.phase == "identity_commit_started":
                    if state.comment not in {None, expected}:
                        raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")
                elif state.comment != expected:
                    raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")
                if state.active_connections:
                    raise RestoreDatabaseError("RESTORE_TARGET_IN_USE")
                if state.prepared_transactions:
                    raise RestoreDatabaseError("RESTORE_TARGET_PREPARED_XACT")
                try:
                    await adapter.drop_target(record.generated_target_name)
                    dropped = True
                except RestoreDatabaseError:
                    if record.phase == "verification_passed":
                        record = self.store.advance(record, "drop_failed")
                    raise
                if (await adapter.inspect_target(record.generated_target_name)).exists:
                    raise RestoreDatabaseError("RESTORE_DROP_FAILED")
            self.store.delete(record.opaque_id)
            return RestoreCleanupResult(
                status="pass", backup_id=record.backup_id,
                cleanup_handle=record.opaque_id,
                target_dropped="yes" if dropped else "no",
                record_deleted="yes",
            )
        except (RestoreDatabaseError, RestoreRecoveryError) as exc:
            return RestoreCleanupResult(
                status="fail", backup_id=record.backup_id,
                cleanup_handle=record.opaque_id,
                target_dropped="yes" if dropped else "no",
                record_deleted="no", error_code=exc.error_code,
            )
        finally:
            await adapter.aclose()

    async def _run_locked(
        self, root: Path, backup_id: str
    ) -> RestoreVerificationResult:
        validation = await self.validator.validate_locked(backup_id)
        if validation.status != "pass":
            return self._failed(backup_id, validation.error_code or "BACKUP_MANIFEST_INVALID")
        manifest = self._load_manifest(root / backup_id / "manifest.json")
        expected_head = self._expected_head()
        if manifest.alembic_revision != expected_head:
            return self._failed(backup_id, "RESTORE_CODE_REVISION_MISMATCH")
        pg_restore = self.pg_restore_path or self._resolve_pg_restore()
        version = await self.runner.run(
            [pg_restore, "--version"], env=self._tool_env(pg_restore.parent)
        )
        if version.returncode:
            return self._failed(backup_id, "RESTORE_TOOL_MISSING")
        restore_major = parse_pg_major(version.stdout)
        spec = parse_pg_connection_spec(self.settings.DATABASE_URL)
        adapter = self.adapter_factory(self.settings.DATABASE_URL, spec)
        record: RestoreRecoveryRecord | None = None
        target_created = False
        restore_completed = False
        schema_verified = False
        constraints_verified = False
        integrity_verified = False
        target_dropped = False
        try:
            target_major = await adapter.server_major()
            try:
                require_restore_version_compatibility(
                    source_server_major=manifest.database.source_server_major,
                    pg_dump_major=manifest.database.pg_dump_major,
                    pg_restore_major=restore_major,
                    restore_target_server_major=target_major,
                )
            except ValueError as exc:
                raise RestoreDatabaseError("RESTORE_VERSION_UNSUPPORTED") from exc
            target, opaque_id, token = generate_restore_identity(
                datetime.now(timezone.utc)
            )
            record = RestoreRecoveryRecord(
                opaque_id=opaque_id,
                generated_target_name=target,
                identity_token=token,
                created_at=datetime.now(timezone.utc),
                backup_id=backup_id,
                phase="planned",
            )
            self.store.write(record)
            record = self.store.advance(record, "create_started")
            await adapter.create_target(target)
            target_created = True
            self._require_created(await adapter.inspect_target(target))
            record = self.store.advance(record, "database_created")
            record = self.store.advance(record, "identity_commit_started")
            await adapter.commit_identity(target, token)
            self._require_identified(await adapter.inspect_target(target), token)
            record = self.store.advance(record, "identity_committed")
            if not await adapter.current_database_is(target):
                raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")
            record = self.store.advance(record, "restore_started")
            env = build_libpq_env(
                spec.model_copy(update={"database": target}),
                parent_env=os.environ,
                path=str(pg_restore.parent),
            )
            restore = await self.runner.run(
                [
                    pg_restore,
                    f"--dbname={target}",
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                    root / backup_id / "database.dump",
                ],
                env=env,
            )
            if restore.returncode:
                record = self.store.advance(record, "restore_failed")
                return self._failed_with_record(
                    record, "RESTORE_FAILED", target_created=True
                )
            restore_completed = True
            self._require_identified(await adapter.inspect_target(target), token)
            verifier = self.verifier_factory(self.settings.DATABASE_URL)
            try:
                summary = await verifier.verify(
                    target, expected_revision=manifest.alembic_revision
                )
            except RestoreDatabaseError:
                record = self.store.advance(record, "verification_failed")
                raise
            schema_verified = summary.schema_verified
            constraints_verified = summary.constraints_verified
            integrity_verified = summary.integrity_verified
            record = self.store.advance(record, "verification_passed")
            self._require_drop_safe(await adapter.inspect_target(target), token)
            try:
                await adapter.drop_target(target)
            except RestoreDatabaseError:
                record = self.store.advance(record, "drop_failed")
                raise
            if (await adapter.inspect_target(target)).exists:
                record = self.store.advance(record, "drop_failed")
                raise RestoreDatabaseError("RESTORE_DROP_FAILED")
            target_dropped = True
            self.store.delete(record.opaque_id)
            try:
                manifest_sha256, _ = stream_sha256(
                    root / backup_id / "manifest.json"
                )
                self.verification_store.write_passed(
                    manifest=manifest,
                    manifest_sha256=manifest_sha256,
                )
            except (BackupFsError, BackupVerificationError):
                return RestoreVerificationResult(
                    status="fail",
                    backup_id=backup_id,
                    target_created="yes",
                    restore_completed="yes",
                    schema_verified="yes",
                    constraints_verified="yes",
                    integrity_verified="yes",
                    target_dropped="yes",
                    cleanup_required="no",
                    restore_timeout_seconds=self.timeout,
                    error_code="BACKUP_VERIFICATION_WRITE_FAILED",
                )
            return RestoreVerificationResult(
                status="pass",
                backup_id=backup_id,
                target_created="yes",
                restore_completed="yes",
                schema_verified="yes",
                constraints_verified="yes",
                integrity_verified="yes",
                target_dropped="yes",
                cleanup_required="no",
                restore_timeout_seconds=self.timeout,
            )
        except asyncio.CancelledError:
            if record is not None and record.phase == "restore_started":
                try:
                    self.store.advance(record, "restore_failed")
                except RestoreRecoveryError:
                    pass
            raise
        except BackupServiceError as exc:
            code = "RESTORE_TIMEOUT" if exc.error_code == "PG_TOOL_TIMEOUT" else exc.error_code
            return self._failed_with_optional_record(
                backup_id, record, code, target_created, restore_completed,
                schema_verified, constraints_verified, integrity_verified,
                target_dropped,
            )
        except (RestoreDatabaseError, RestoreRecoveryError) as exc:
            return self._failed_with_optional_record(
                backup_id, record, exc.error_code, target_created, restore_completed,
                schema_verified, constraints_verified, integrity_verified,
                target_dropped,
            )
        finally:
            await adapter.aclose()

    def _load_manifest(self, path: Path) -> BackupManifest:
        try:
            return BackupManifest.model_validate(
                json.loads(read_regular_exact(path, max_bytes=MAX_MANIFEST_BYTES))
            )
        except Exception as exc:
            raise BackupServiceError("BACKUP_MANIFEST_INVALID") from exc

    def _expected_head(self) -> str:
        from app.deploy.backup_service import _expected_alembic_head

        return _expected_alembic_head(self.repository_root / "backend")

    @staticmethod
    def _require_created(state: object) -> None:
        if not state.exists or not state.owner_matches or state.comment is not None:
            raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")

    @staticmethod
    def _require_identified(state: object, token: str) -> None:
        if (
            not state.exists
            or not state.owner_matches
            or state.comment != f"{IDENTITY_PREFIX}{token}"
        ):
            raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")

    @classmethod
    def _require_drop_safe(cls, state: object, token: str) -> None:
        cls._require_identified(state, token)
        if state.active_connections:
            raise RestoreDatabaseError("RESTORE_TARGET_IN_USE")
        if state.prepared_transactions:
            raise RestoreDatabaseError("RESTORE_TARGET_PREPARED_XACT")

    @staticmethod
    def _resolve_pg_restore() -> Path:
        resolved = shutil.which("pg_restore")
        if resolved is None:
            raise BackupServiceError("RESTORE_TOOL_MISSING")
        return Path(resolved).resolve()

    @staticmethod
    def _tool_env(tool_dir: Path) -> dict[str, str]:
        env = {"PATH": str(tool_dir)}
        for key in ("LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT"):
            if value := os.environ.get(key):
                env[key] = value
        return env

    def _failed(self, backup_id: str, code: str) -> RestoreVerificationResult:
        safe_backup_id: str | None = backup_id
        try:
            validate_backup_id(backup_id)
        except ValueError:
            safe_backup_id = None
        return RestoreVerificationResult(
            status="fail", backup_id=safe_backup_id,
            target_created="no", restore_completed="no", schema_verified="no",
            constraints_verified="no", integrity_verified="no",
            target_dropped="no", cleanup_required="no",
            restore_timeout_seconds=max(1, self.timeout), error_code=code,
        )

    def _failed_with_record(
        self, record: RestoreRecoveryRecord, code: str, *, target_created: bool
    ) -> RestoreVerificationResult:
        return self._failed_with_optional_record(
            record.backup_id, record, code, target_created, False, False, False,
            False, False,
        )

    def _failed_with_optional_record(
        self, backup_id: str, record: RestoreRecoveryRecord | None, code: str,
        target_created: bool, restore_completed: bool, schema_verified: bool,
        constraints_verified: bool, integrity_verified: bool, target_dropped: bool,
    ) -> RestoreVerificationResult:
        cleanup = record is not None and (
            target_created or record.phase != "planned"
        )
        return RestoreVerificationResult(
            status="fail", backup_id=backup_id,
            target_created="yes" if target_created else "no",
            restore_completed="yes" if restore_completed else "no",
            schema_verified="yes" if schema_verified else "no",
            constraints_verified="yes" if constraints_verified else "no",
            integrity_verified="yes" if integrity_verified else "no",
            target_dropped="yes" if target_dropped else "no",
            cleanup_required="yes" if cleanup else "no",
            cleanup_handle=record.opaque_id if cleanup else None,
            restore_timeout_seconds=self.timeout, error_code=code,
        )


async def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    service = RestoreVerificationService(load_settings())
    try:
        if len(args) == 3 and args[:2] == ["run", "--backup-id"]:
            validate_backup_id(args[2])
            result = await service.run(args[2])
        elif (
            len(args) == 5
            and args[0] == "cleanup"
            and args[1] == "--backup-id"
            and args[3] == "--recovery-record"
        ):
            validate_backup_id(args[2])
            result = await service.cleanup(
                backup_id=args[2], recovery_record=args[4]
            )
        else:
            raise ValueError("invalid arguments")
    except ValueError:
        print(
            "usage: python -m app.deploy.restore_verify "
            "run --backup-id <backup_id> | cleanup --backup-id <backup_id> "
            "--recovery-record <opaque-id>"
        )
        return 2
    print(result.model_dump_json())
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
