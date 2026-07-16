"""Authorized generated-PostgreSQL happy-path rehearsal for P6-Deploy-4D-4C-2."""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.deploy.backup_fs import stream_sha256
from app.deploy.backup_models import PgConnectionSpec
from app.deploy.backup_service import PgToolRunner, required_catalog_objects_present
from app.deploy.production_recovery_models import (
    AuthorizationObservations,
    ProductionCleanupRecord,
    ProductionRecoveryRecord,
    ResourceIdentities,
)
from app.deploy.production_recovery_postgres import (
    TempPostgresRehearsalError,
    build_dump_command,
    build_restore_command,
    switch_database_component,
)
from app.deploy.production_recovery_postgres_models import TempPostgresRehearsalResult
from app.deploy.production_recovery_postgres_real import (
    DurableReplacementWorkflowGuard,
    FrozenObservationProvider,
    RealServerRoleObservationProvider,
    RealTempPostgresAdapter,
    _database_url,
)
from app.deploy.production_recovery_postgres_record import (
    TempPostgresRehearsalRecordStore,
)
from app.deploy.production_recovery_record import (
    TempRecoveryRecordStore,
    create_temp_recovery_capability,
)
from app.deploy.restore_database import RestoreDatabaseVerifier

UTC = timezone.utc
SYNTHETIC_CHANNEL_ID = 900000001
SYNTHETIC_MESSAGE_ID = 900000002


@dataclass(frozen=True)
class HappyPathInputs:
    root: Path
    backend_root: Path
    spec: PgConnectionSpec
    pg_dump: Path
    pg_restore: Path


class TempApplicationFixture:
    """Own one bounded generated-database connection until explicit stop."""

    def __init__(self, *, spec: PgConnectionSpec, application_name: str) -> None:
        self.spec = spec
        self.application_name = application_name
        self.engine: AsyncEngine | None = None
        self.connection: AsyncConnection | None = None

    async def start(self, target: PgConnectionSpec) -> None:
        if self.connection is not None:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_APP_STATE_INVALID")
        self.engine = create_async_engine(
            _database_url(target, target.database),
            connect_args={"server_settings": {"application_name": self.application_name}},
        )
        try:
            self.connection = await self.engine.connect()
        except Exception:
            await self.stop()
            raise

    async def readiness(self, *, database: str, revision: str) -> None:
        if self.connection is None:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_APP_STATE_INVALID")
        transaction = await self.connection.begin()
        try:
            await self.connection.execute(text("SET TRANSACTION READ ONLY"))
            current = await self.connection.scalar(text("SELECT current_database()"))
            observed = await self.connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            smoke = await self.connection.scalar(
                text("SELECT count(*) FROM raw_messages")
            )
        finally:
            await transaction.rollback()
        if current != database or observed != revision or smoke != 1:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_READINESS_FAILED")

    async def stop(self) -> None:
        if self.connection is not None:
            await self.connection.close()
            self.connection = None
        if self.engine is not None:
            await self.engine.dispose()
            self.engine = None

    async def require_drained(self) -> None:
        engine = create_async_engine(_database_url(self.spec, self.spec.database))
        try:
            async with engine.connect() as connection:
                count = await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE application_name=:application_name"
                    ),
                    {"application_name": self.application_name},
                )
            if count != 0:
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_APP_NOT_DRAINED")
        finally:
            await engine.dispose()


class GeneratedPostgresHappyPath:
    """Single owner for generated databases, subprocesses, records, and cleanup."""

    def __init__(self, inputs: HappyPathInputs) -> None:
        self.inputs = inputs
        self.provider = RealServerRoleObservationProvider()
        self.tool_runner = PgToolRunner(timeout_seconds=120)

    async def run(self) -> TempPostgresRehearsalResult:
        capability = await self.provider.issue_initial(
            root=self.inputs.root, spec=self.inputs.spec
        )
        rehearsal_store = TempPostgresRehearsalRecordStore(capability)
        rehearsal = rehearsal_store.create()
        workflow_store = TempRecoveryRecordStore(
            create_temp_recovery_capability(self.inputs.root)
        )
        adapter = RealTempPostgresAdapter(
            capability=capability,
            spec=self.inputs.spec,
            provider=self.provider,
            record_store=rehearsal_store,
            workflow_guard=DurableReplacementWorkflowGuard(workflow_store),
        )
        app = TempApplicationFixture(
            spec=self.inputs.spec,
            application_name=(
                "tg-hub-4c-app-"
                + hashlib.sha256(capability.run_id.encode()).hexdigest()[:12]
            ),
        )
        workflow: ProductionRecoveryRecord | None = None
        dump = self.inputs.root / "synthetic.dump"
        try:
            rehearsal = rehearsal_store.advance(rehearsal, "source_create_started")
            await adapter.create(capability.source_database)
            rehearsal = rehearsal_store.advance(rehearsal, "source_created")
            rehearsal = rehearsal_store.advance(
                rehearsal, "source_identity_commit_started"
            )
            await adapter.commit_identity(capability.source_database)
            rehearsal = rehearsal_store.advance(
                rehearsal, "source_identity_committed"
            )

            expected_revision = await self._migrate_and_seed(capability.source_database)
            rehearsal = rehearsal_store.advance(rehearsal, "dump_started")
            await self._dump(capability, dump)
            dump_sha256, _ = stream_sha256(dump)
            rehearsal = rehearsal_store.update_facts(
                rehearsal, dump_identity=dump_sha256
            )
            rehearsal = rehearsal_store.advance(rehearsal, "dump_committed")

            workflow = workflow_store.create(
                self._new_workflow(capability, expected_revision, dump_sha256)
            )
            rehearsal = rehearsal_store.update_facts(
                rehearsal, workflow_record_id=workflow.incident_id
            )
            rehearsal = rehearsal_store.advance(rehearsal, "replacement_bound")

            workflow = self._skip_protection(workflow_store, workflow)
            workflow = workflow_store.advance(workflow, "replacement_create_started")
            await adapter.create(capability.replacement_database)
            workflow = workflow_store.advance(workflow, "replacement_created")
            workflow = workflow_store.advance(workflow, "identity_commit_started")
            await adapter.commit_identity(capability.replacement_database)
            workflow = workflow_store.advance(workflow, "identity_committed")
            workflow = workflow_store.advance(workflow, "restore_started")
            await self._restore(capability, dump)
            workflow = workflow_store.advance(workflow, "restore_completed")
            workflow = workflow_store.advance(workflow, "verification_started")
            await self._verify(capability.replacement_database, expected_revision)
            workflow = workflow_store.update_facts(
                workflow, verification_result="passed"
            )
            workflow = workflow_store.advance(workflow, "verification_completed")

            workflow = await self._switch_and_rollback(
                workflow_store, workflow, capability, expected_revision, app
            )
            rehearsal = rehearsal_store.advance(rehearsal, "workflow_terminal")
            rehearsal = await self._cleanup_replacement(
                rehearsal_store, rehearsal, workflow_store, workflow
            )
            rehearsal = rehearsal_store.advance(rehearsal, "source_cleanup_started")
            await app.stop()
            await app.require_drained()
            resume = rehearsal_store.issue_resume_cleanup(
                spec=self.inputs.spec,
                provider=FrozenObservationProvider(
                    await self.provider.observe(self.inputs.spec)
                ),
            )
            source_adapter = RealTempPostgresAdapter(
                capability=resume,
                spec=self.inputs.spec,
                provider=self.provider,
                record_store=rehearsal_store,
                workflow_guard=DurableReplacementWorkflowGuard(workflow_store),
            )
            try:
                await source_adapter.drop(resume.source_database)
                source_state = await source_adapter.inspect(
                    resume.source_database, include_catalog=False
                )
                replacement_state = await source_adapter.inspect(
                    resume.replacement_database, include_catalog=False
                )
            finally:
                await source_adapter.aclose()
            if source_state.exists or replacement_state.exists:
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_RESIDUE_PRESENT")
            rehearsal = rehearsal_store.update_facts(
                rehearsal, source_cleanup_status="completed", residue_count=0
            )
            rehearsal = rehearsal_store.advance(rehearsal, "source_cleanup_completed")
            rehearsal = rehearsal_store.advance(rehearsal, "rehearsal_terminal")
            return TempPostgresRehearsalResult(
                status="success",
                run_id=rehearsal.run_id,
                phase=rehearsal.phase,
                source_created=True,
                dump_validated=True,
                replacement_created=True,
                restore_completed=True,
                verification_completed=True,
                config_switched=True,
                rollback_completed=True,
                replacement_cleanup_completed=True,
                source_cleanup_completed=True,
                residue_count=0,
            )
        finally:
            await app.stop()
            await adapter.aclose()

    async def _migrate_and_seed(self, database: str) -> str:
        backend = self.inputs.backend_root.resolve()
        config = Config(str(backend / "alembic.ini"))
        config.set_main_option("script_location", str(backend / "alembic"))
        revision = ScriptDirectory.from_config(config).get_current_head()
        if revision is None:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_MIGRATION_FAILED")
        env = {
            "DATABASE_URL": _database_url(self.inputs.spec, database).render_as_string(
                hide_password=False
            ),
            "TG_HUB_ENV_FILE": str(self.inputs.root / "empty.env"),
            "PYTHONPATH": str(backend),
            "PATH": "/usr/bin:/bin",
        }
        alembic = backend.parent / ".venv" / "bin" / "alembic"
        result = await self.tool_runner.run(
            (alembic, "-c", backend / "alembic.ini", "upgrade", "head"),
            env=env,
            cwd=backend,
        )
        if result.returncode:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_MIGRATION_FAILED")
        engine = create_async_engine(_database_url(self.inputs.spec, database))
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO channels "
                        "(name, tg_id, source_type, status) "
                        "VALUES ('synthetic-4c', :tg_id, 'telegram', 'active')"
                    ),
                    {"tg_id": SYNTHETIC_CHANNEL_ID},
                )
                channel_id = await connection.scalar(
                    text("SELECT id FROM channels WHERE tg_id=:tg_id"),
                    {"tg_id": SYNTHETIC_CHANNEL_ID},
                )
                await connection.execute(
                    text(
                        "INSERT INTO raw_messages "
                        "(channel_id, tg_message_id, raw_text, ingest_status, "
                        "parse_status, dedup_status, parse_attempts) "
                        "VALUES (:channel_id, :message_id, 'synthetic-4c', "
                        "'stored', 'parse_pending', 'dedup_pending', 0)"
                    ),
                    {"channel_id": channel_id, "message_id": SYNTHETIC_MESSAGE_ID},
                )
            async with engine.connect() as connection:
                observed = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                count = await connection.scalar(text("SELECT count(*) FROM raw_messages"))
            if observed != revision or count != 1:
                raise TempPostgresRehearsalError("TEMP_REHEARSAL_SOURCE_INVALID")
        finally:
            await engine.dispose()
        return revision

    async def _dump(self, capability, dump: Path) -> None:
        command = build_dump_command(
            capability,
            spec=self.inputs.spec,
            pg_dump=self.inputs.pg_dump,
            dump=dump,
        )
        result = await self.tool_runner.run(command.argv, env=command.env)
        if result.returncode or not dump.is_file():
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_DUMP_FAILED")
        os.chmod(dump, 0o600)
        catalog = await self.tool_runner.run(
            (self.inputs.pg_restore, "--list", dump),
            env={"PATH": "/usr/bin:/bin"},
        )
        if catalog.returncode or not required_catalog_objects_present(catalog.stdout):
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_DUMP_INVALID")

    async def _restore(self, capability, dump: Path) -> None:
        command = build_restore_command(
            capability,
            spec=self.inputs.spec,
            pg_restore=self.inputs.pg_restore,
            dump=dump,
        )
        result = await self.tool_runner.run(command.argv, env=command.env)
        if result.returncode:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_RESTORE_FAILED")

    async def _verify(self, database: str, revision: str) -> None:
        base_url = _database_url(self.inputs.spec, self.inputs.spec.database).render_as_string(
            hide_password=False
        )
        summary = await RestoreDatabaseVerifier(
            base_url, generated_rehearsal_only=True
        ).verify(
            database, expected_revision=revision
        )
        if not (summary.schema_verified and summary.constraints_verified and summary.integrity_verified):
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_VERIFICATION_FAILED")

    def _new_workflow(self, capability, revision: str, dump_sha256: str) -> ProductionRecoveryRecord:
        digest = hashlib.sha256(capability.run_id.encode()).hexdigest()
        backup_id = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ-")
            + capability.run_id
        )
        resources = ResourceIdentities(
            original_database_revision=revision,
            original_database_component=capability.source_database,
            original_database_identity=capability.source_database,
            original_database_owner=str(capability.role_oid),
            replacement_database_identity=capability.replacement_database,
            replacement_identity_token=capability.replacement_token,
            expected_owner_identity=str(capability.role_oid),
            selected_manifest_sha256=digest,
            selected_database_dump_sha256=dump_sha256,
            selected_watchlist_sha256=digest,
            original_env_sha256=digest,
            staged_env_sha256=dump_sha256,
            original_watchlist_sha256=digest,
        )
        return ProductionRecoveryRecord(
            incident_id=secrets.token_hex(16),
            selected_backup_id=backup_id,
            resources=resources,
        )

    @staticmethod
    def _skip_protection(
        store: TempRecoveryRecordStore,
        record: ProductionRecoveryRecord,
    ) -> ProductionRecoveryRecord:
        auth = AuthorizationObservations(protection_skip_authorized="yes")
        record = store.update_facts(
            record,
            authorizations=auth,
            protection_backup_status="skipped_authorized",
        )
        return store.advance(record, "protection_backup_skipped_authorized")

    async def _switch_and_rollback(
        self, store, record, capability, revision, app: TempApplicationFixture
    ):
        stop_facts = dict(
            monitor_stopped="yes",
            session_lease_free="yes",
            application_stopped="yes",
            application_connections_drained="yes",
        )
        source = switch_database_component(
            capability, spec=self.inputs.spec, target="source"
        )
        await app.start(source)
        await app.readiness(database=source.database, revision=revision)
        record = store.advance(record, "services_stop_started")
        await app.stop()
        await app.require_drained()
        record = store.update_facts(record, **stop_facts)
        record = store.advance(record, "services_stopped")
        record = store.advance(record, "config_protection_started")
        resources = record.resources.model_copy(
            update={
                "protected_env_sha256": record.resources.original_env_sha256,
                "protected_watchlist_sha256": record.resources.original_watchlist_sha256,
            }
        )
        record = store.update_facts(record, resources=resources)
        record = store.advance(record, "config_protection_completed")
        record = store.advance(record, "env_switch_started")
        replacement = switch_database_component(
            capability, spec=self.inputs.spec, target="replacement"
        )
        record = store.update_facts(record, replacement_activated="yes")
        record = store.advance(record, "env_switched")
        record = store.advance(record, "config_switched")
        record = store.advance(record, "application_start_started")
        await app.start(replacement)
        record = store.advance(record, "application_started")
        record = store.advance(record, "readiness_started")
        await app.readiness(database=replacement.database, revision=revision)
        record = store.advance(record, "readiness_passed")
        record = store.advance(record, "rollback_started")
        record = store.advance(record, "rollback_monitor_stopped")
        record = store.advance(record, "rollback_application_stop_started")
        await app.stop()
        await app.require_drained()
        record = store.advance(record, "rollback_application_stopped")
        record = store.advance(record, "rollback_env_started")
        record = store.advance(record, "rollback_env_completed")
        record = store.advance(record, "rollback_application_start_started")
        await app.start(source)
        record = store.advance(record, "rollback_application_started")
        record = store.advance(record, "rollback_readiness_started")
        await app.readiness(database=source.database, revision=revision)
        record = store.advance(record, "rollback_readiness_passed")
        return store.advance(record, "rolled_back")

    async def _cleanup_replacement(
        self, rehearsal_store, rehearsal, workflow_store, workflow
    ):
        cleanup_id = hashlib.sha256(
            f"{workflow.incident_id}:replacement-cleanup".encode()
        ).hexdigest()[:32]
        workflow = workflow_store.update_facts(
            workflow, cleanup_requested="yes", cleanup_record_id=cleanup_id
        )
        if workflow.stable_lock_identity is None:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_WORKFLOW_RECORD_INVALID")
        child = ProductionCleanupRecord(
            cleanup_record_id=cleanup_id,
            incident_id=workflow.incident_id,
            replacement_database_identity=(
                workflow.resources.replacement_database_identity
            ),
            replacement_identity_token=workflow.resources.replacement_identity_token,
            expected_owner_identity=workflow.resources.expected_owner_identity,
            stable_lock_identity=workflow.stable_lock_identity,
        )
        workflow_store.create_cleanup(workflow, child)
        child = workflow_store.advance_cleanup(workflow, child, "cleanup_started")
        resume = rehearsal_store.issue_resume_cleanup(
            spec=self.inputs.spec,
            provider=FrozenObservationProvider(
                await self.provider.observe(self.inputs.spec)
            ),
        )
        adapter = RealTempPostgresAdapter(
            capability=resume,
            spec=self.inputs.spec,
            provider=self.provider,
            record_store=rehearsal_store,
            workflow_guard=DurableReplacementWorkflowGuard(workflow_store),
        )
        try:
            await adapter.drop(resume.replacement_database)
        finally:
            await adapter.aclose()
        child = workflow_store.update_cleanup_facts(
            workflow, child, drop_observed="yes"
        )
        child = workflow_store.advance_cleanup(workflow, child, "cleanup_completed")
        workflow_store.update_facts(workflow, cleanup_completed="yes")
        return rehearsal_store.update_facts(
            rehearsal, replacement_cleanup_status="completed"
        )


def discover_inputs(root: Path) -> HappyPathInputs:
    backend = Path(__file__).resolve().parents[2]
    pg_dump = Path(shutil.which("pg_dump") or "")
    pg_restore = Path(shutil.which("pg_restore") or "")
    if not pg_dump.is_absolute() or not pg_restore.is_absolute():
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_TOOL_MISSING")
    return HappyPathInputs(
        root=root,
        backend_root=backend,
        spec=PgConnectionSpec(
            host="127.0.0.1",
            port=5432,
            user="tg_hub_4c_rehearsal",
            password=None,
            database="postgres",
        ),
        pg_dump=pg_dump,
        pg_restore=pg_restore,
    )


async def main(root: Path) -> TempPostgresRehearsalResult:
    return await GeneratedPostgresHappyPath(discover_inputs(root)).run()
