"""Authorized 4C-2 PostgreSQL adapter; importing it performs no I/O."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.deploy.backup_models import PgConnectionSpec
from app.deploy.production_recovery_postgres import (
    ServerRoleObservation,
    TempPostgresCapabilityIssuer,
    TempPostgresRehearsalCapability,
    TempPostgresRehearsalError,
    _create_trusted_observation,
    _require_connection_binding,
    _require_database_action,
    validate_capability,
    validate_connection_policy,
    validate_temp_root,
)
from app.deploy.production_recovery_postgres_models import (
    CatalogObject,
    LiveGeneratedDatabaseState,
    classify_empty_catalog,
)
from app.deploy.production_recovery_postgres_record import (
    TempPostgresRehearsalRecordStore,
)
from app.deploy.production_recovery_record import TempRecoveryRecordStore

EngineFactory = Callable[..., AsyncEngine]
MAINTENANCE_DATABASE = "postgres"
COMMENT_PREFIX = "tg-hub-production-recovery-rehearsal:"
CONNECT_TIMEOUT_SECONDS = 10.0
COMMAND_TIMEOUT_SECONDS = 30.0


class ReplacementWorkflowGuard(Protocol):
    def require(
        self,
        *,
        workflow_record_id: str,
        action: Literal["create", "commit_identity", "drop"],
        capability: TempPostgresRehearsalCapability,
    ) -> None: ...


class DurableReplacementWorkflowGuard:
    """Bind replacement operations to the existing 4B main/child records."""

    def __init__(self, store: TempRecoveryRecordStore) -> None:
        self.store = store

    def require(
        self,
        *,
        workflow_record_id: str,
        action: Literal["create", "commit_identity", "drop"],
        capability: TempPostgresRehearsalCapability,
    ) -> None:
        try:
            main = self.store.read(workflow_record_id)
            if (
                main.resources.replacement_database_identity
                != capability.replacement_database
                or main.resources.replacement_identity_token
                != capability.replacement_token
            ):
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_WORKFLOW_IDENTITY_INVALID"
                )
            if action == "create" and main.phase != "replacement_create_started":
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_WORKFLOW_PHASE_INVALID"
                )
            if action == "commit_identity" and main.phase != "identity_commit_started":
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_WORKFLOW_PHASE_INVALID"
                )
            if action == "drop":
                if main.phase != "rolled_back" or main.cleanup_requested != "yes":
                    raise TempPostgresRehearsalError(
                        "TEMP_REHEARSAL_WORKFLOW_PHASE_INVALID"
                    )
                child = self.store.read_cleanup(main)
                if child.phase != "cleanup_started" or child.drop_observed != "no":
                    raise TempPostgresRehearsalError(
                        "TEMP_REHEARSAL_WORKFLOW_PHASE_INVALID"
                    )
        except TempPostgresRehearsalError:
            raise
        except Exception as exc:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_WORKFLOW_RECORD_INVALID"
            ) from exc


def _database_url(spec: PgConnectionSpec, database: str) -> URL:
    return URL.create(
        "postgresql+asyncpg",
        username=spec.user,
        password=spec.password,
        host=spec.host,
        port=spec.port,
        database=database,
    )


class RealServerRoleObservationProvider:
    def __init__(
        self,
        *,
        engine_factory: EngineFactory = create_async_engine,
        overall_timeout_seconds: float = 45.0,
    ) -> None:
        if overall_timeout_seconds <= 0:
            raise ValueError("overall timeout must be positive")
        self.engine_factory = engine_factory
        self.overall_timeout_seconds = overall_timeout_seconds

    async def observe(self, spec: PgConnectionSpec) -> ServerRoleObservation:
        try:
            async with asyncio.timeout(self.overall_timeout_seconds):
                return await self._observe_owned(spec)
        except TimeoutError as exc:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_PREFLIGHT_TIMEOUT"
            ) from exc

    async def _observe_owned(
        self, spec: PgConnectionSpec
    ) -> ServerRoleObservation:
        validate_connection_policy(spec)
        if spec.database != MAINTENANCE_DATABASE:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")
        engine = self.engine_factory(
            _database_url(spec, MAINTENANCE_DATABASE),
            pool_pre_ping=True,
            connect_args={
                "timeout": CONNECT_TIMEOUT_SECONDS,
                "command_timeout": COMMAND_TIMEOUT_SECONDS,
            },
        )
        try:
            async with engine.connect() as connection:
                current_database = await connection.scalar(
                    text("SELECT current_database()")
                )
                version_num = await connection.scalar(text("SHOW server_version_num"))
                system_identifier = await connection.scalar(
                    text("SELECT system_identifier::text FROM pg_control_system()")
                )
                role = (
                    await connection.execute(
                        text(
                            "SELECT oid, rolname, rolcreatedb, rolsuper "
                            "FROM pg_roles WHERE rolname = current_user"
                        )
                    )
                ).one()
            if current_database != MAINTENANCE_DATABASE:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_CONNECTION_UNSAFE"
                )
            server_digest = hashlib.sha256(str(system_identifier).encode()).hexdigest()
            role_digest = hashlib.sha256(
                f"{int(role.oid)}:{str(role.rolname)}".encode()
            ).hexdigest()
            return _create_trusted_observation(
                server_major=int(str(version_num)) // 10000,
                server_identity_digest=server_digest,
                role_oid=int(role.oid),
                role_identity_digest=role_digest,
                role_can_create_database=bool(role.rolcreatedb),
                role_is_superuser=bool(role.rolsuper),
            )
        except TempPostgresRehearsalError:
            raise
        except Exception as exc:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_PREFLIGHT_FAILED"
            ) from exc
        finally:
            await engine.dispose()

    async def issue_initial(
        self,
        *,
        root: Path,
        spec: PgConnectionSpec,
    ) -> TempPostgresRehearsalCapability:
        resolved, device, inode = validate_temp_root(root)
        observation = await self.observe(spec)
        return TempPostgresCapabilityIssuer()._issue_initial_observed(
            root=resolved,
            device=device,
            inode=inode,
            spec=spec,
            observation=observation,
        )


class FrozenObservationProvider:
    """Bridges one already-authorized async observation into the locked store API."""

    def __init__(self, observation: ServerRoleObservation) -> None:
        self.observation = observation

    def observe(self, _spec: PgConnectionSpec) -> ServerRoleObservation:
        return self.observation


class RealTempPostgresAdapter:
    def __init__(
        self,
        *,
        capability: TempPostgresRehearsalCapability,
        spec: PgConnectionSpec,
        provider: RealServerRoleObservationProvider,
        record_store: TempPostgresRehearsalRecordStore,
        workflow_guard: ReplacementWorkflowGuard,
        engine_factory: EngineFactory = create_async_engine,
    ) -> None:
        validate_capability(capability)
        validate_connection_policy(spec)
        _require_connection_binding(capability, spec)
        if spec.database != MAINTENANCE_DATABASE:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_CONNECTION_UNSAFE")
        self.capability = capability
        self.spec = spec
        self.provider = provider
        self.record_store = record_store
        self.workflow_guard = workflow_guard
        self.engine_factory = engine_factory
        self.engine = engine_factory(
            _database_url(spec, MAINTENANCE_DATABASE),
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
            connect_args={
                "timeout": CONNECT_TIMEOUT_SECONDS,
                "command_timeout": COMMAND_TIMEOUT_SECONDS,
            },
        )

    async def create(self, database: str) -> None:
        _require_database_action(self.capability, database, action="create")
        self._require_record_action(database, action="create")
        await self._require_live_identity()
        state = await self.inspect(database, include_catalog=False)
        if state.exists:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_DATABASE_EXISTS")
        await self._formatted_ddl(
            "SELECT format('CREATE DATABASE %I OWNER %I', "
            "CAST(:database AS text), current_user)",
            {"database": database},
            "TEMP_REHEARSAL_SOURCE_CREATE_FAILED",
        )
        created = await self.inspect(database, include_catalog=False)
        if (
            not created.exists
            or created.owner_oid != self.capability.role_oid
            or created.comment is not None
            or created.active_connections != 0
            or created.prepared_transactions != 0
        ):
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_DATABASE_IDENTITY_MISMATCH"
            )

    async def commit_identity(self, database: str) -> None:
        _require_database_action(
            self.capability, database, action="commit_identity"
        )
        self._require_record_action(database, action="commit_identity")
        await self._require_live_identity()
        token = self._token_for(database)
        role = (
            "source"
            if database == self.capability.source_database
            else "replacement"
        )
        comment = (
            f"{COMMENT_PREFIX}{self.capability.run_id}:{role}:{token}"
        )
        state = await self.inspect(database, include_catalog=False)
        if (
            not state.exists
            or state.owner_oid != self.capability.role_oid
            or state.active_connections != 0
            or state.prepared_transactions != 0
        ):
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_DATABASE_IDENTITY_MISMATCH"
            )
        if state.comment not in {None, comment}:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_DATABASE_IDENTITY_MISMATCH"
            )
        if state.comment is None:
            await self._formatted_ddl(
                "SELECT format('COMMENT ON DATABASE %I IS %L', "
                "CAST(:database AS text), CAST(:comment AS text))",
                {"database": database, "comment": comment},
                "TEMP_REHEARSAL_IDENTITY_COMMIT_FAILED",
            )
        confirmed = await self.inspect(database, include_catalog=False)
        if confirmed.comment != comment:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_IDENTITY_COMMIT_FAILED"
            )

    async def inspect(
        self, database: str, *, include_catalog: bool = True
    ) -> LiveGeneratedDatabaseState:
        self._guard_generated_name(database)
        query = text(
            """
            SELECT d.datname IS NOT NULL AS exists,
                   d.datdba AS owner_oid,
                   shobj_description(d.oid, 'pg_database') AS comment,
                   COALESCE((SELECT count(*) FROM pg_stat_activity a
                             WHERE a.datname = d.datname
                               AND a.pid <> pg_backend_pid()), 0) AS active_connections,
                   COALESCE((SELECT count(*) FROM pg_prepared_xacts p
                             WHERE p.database = d.datname), 0) AS prepared_transactions
              FROM (SELECT CAST(:database AS text) AS requested) q
              LEFT JOIN pg_database d ON d.datname = q.requested
            """
        )
        try:
            async with self.engine.connect() as connection:
                row = (
                    await connection.execute(query, {"database": database})
                ).one()
            exists = bool(row.exists)
            catalog_state = "not_checked"
            if exists and include_catalog:
                catalog_state = await self._catalog_state(database)
            return LiveGeneratedDatabaseState(
                exists=exists,
                owner_oid=int(row.owner_oid) if row.owner_oid is not None else None,
                comment=row.comment,
                active_connections=int(row.active_connections),
                prepared_transactions=int(row.prepared_transactions),
                catalog_state=catalog_state,
            )
        except TempPostgresRehearsalError:
            raise
        except Exception as exc:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_INSPECTION_FAILED"
            ) from exc

    async def drop(self, database: str) -> None:
        _require_database_action(self.capability, database, action="drop")
        self._require_record_action(database, action="drop")
        await self._require_live_identity()
        state = await self.inspect(database, include_catalog=False)
        if not state.exists:
            return
        if (
            state.owner_oid != self.capability.role_oid
            or state.comment != self._comment_for(database)
            or state.active_connections != 0
            or state.prepared_transactions != 0
        ):
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_CLEANUP_GUARD_FAILED"
            )
        await self._formatted_ddl(
            "SELECT format('DROP DATABASE %I', CAST(:database AS text))",
            {"database": database},
            "TEMP_REHEARSAL_CLEANUP_FAILED",
        )
        if (await self.inspect(database, include_catalog=False)).exists:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_CLEANUP_FAILED")

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def _require_live_identity(self) -> None:
        observation = await self.provider.observe(self.spec)
        if (
            not observation.role_can_create_database
            or observation.role_is_superuser
        ):
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_CONNECTION_UNSAFE"
            )
        if (
            observation.server_identity_digest
            != self.capability.server_identity_digest
            or observation.role_oid != self.capability.role_oid
            or observation.role_identity_digest
            != self.capability.role_identity_digest
        ):
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_SERVER_IDENTITY_MISMATCH"
            )

    async def _catalog_state(self, database: str) -> str:
        engine = self.engine_factory(
            _database_url(self.spec, database),
            pool_pre_ping=True,
            connect_args={
                "timeout": CONNECT_TIMEOUT_SECONDS,
                "command_timeout": COMMAND_TIMEOUT_SECONDS,
            },
        )
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    text(
                        """
                        SELECT object_type, schema_name, object_name, user_owned
                          FROM (
                            SELECT 'schema'::text AS object_type,
                                   n.nspname::text AS schema_name,
                                   n.nspname::text AS object_name,
                                   pg_get_userbyid(n.nspowner) = current_user
                                     AS user_owned
                              FROM pg_namespace n
                             WHERE n.nspname NOT LIKE 'pg_toast%'
                            UNION ALL
                            SELECT CASE c.relkind WHEN 'S' THEN 'sequence'
                                                  ELSE 'relation' END,
                                   n.nspname, c.relname,
                                   pg_get_userbyid(c.relowner) = current_user
                              FROM pg_class c
                              JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE n.nspname NOT LIKE 'pg_toast%'
                               AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
                            UNION ALL
                            SELECT 'function', n.nspname, p.proname,
                                   pg_get_userbyid(p.proowner) = current_user
                              FROM pg_proc p
                              JOIN pg_namespace n ON n.oid = p.pronamespace
                             WHERE n.nspname NOT LIKE 'pg_toast%'
                            UNION ALL
                            SELECT 'type', n.nspname, t.typname,
                                   pg_get_userbyid(t.typowner) = current_user
                              FROM pg_type t
                              JOIN pg_namespace n ON n.oid = t.typnamespace
                             WHERE n.nspname NOT LIKE 'pg_toast%'
                            UNION ALL
                            SELECT 'extension', n.nspname, e.extname,
                                   pg_get_userbyid(e.extowner) = current_user
                              FROM pg_extension e
                              JOIN pg_namespace n ON n.oid = e.extnamespace
                          ) objects
                        """
                    )
                )
            objects = tuple(
                CatalogObject(
                    object_type=str(row.object_type),
                    schema_name=str(row.schema_name),
                    name=str(row.object_name),
                    user_owned=bool(row.user_owned),
                )
                for row in rows
            )
            return classify_empty_catalog(objects)
        except Exception as exc:
            raise TempPostgresRehearsalError(
                "TEMP_REHEARSAL_INSPECTION_FAILED"
            ) from exc
        finally:
            await engine.dispose()

    async def _formatted_ddl(
        self,
        formatter: str,
        values: dict[str, Any],
        error_code: str,
    ) -> None:
        try:
            async with self.engine.connect() as connection:
                statement = await connection.scalar(text(formatter), values)
                if not isinstance(statement, str):
                    raise TempPostgresRehearsalError(error_code)
                await connection.execute(text(statement))
        except TempPostgresRehearsalError:
            raise
        except Exception as exc:
            raise TempPostgresRehearsalError(error_code) from exc

    def _guard_generated_name(self, database: str) -> None:
        if database not in {
            self.capability.source_database,
            self.capability.replacement_database,
        } or database in {self.spec.database, MAINTENANCE_DATABASE}:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_NOT_AUTHORIZED")

    def _token_for(self, database: str) -> str:
        self._guard_generated_name(database)
        return (
            self.capability.source_token
            if database == self.capability.source_database
            else self.capability.replacement_token
        )

    def _comment_for(self, database: str) -> str:
        role = (
            "source"
            if database == self.capability.source_database
            else "replacement"
        )
        return (
            f"{COMMENT_PREFIX}{self.capability.run_id}:{role}:"
            f"{self._token_for(database)}"
        )

    def _require_record_action(
        self,
        database: str,
        *,
        action: Literal["create", "commit_identity", "drop"],
    ) -> None:
        record = self.record_store.read_nonblocking()
        if record.run_id != self.capability.run_id:
            raise TempPostgresRehearsalError("TEMP_REHEARSAL_RECORD_INVALID")
        if action == "drop":
            if database == self.capability.replacement_database:
                if record.workflow_record_id is None:
                    raise TempPostgresRehearsalError(
                        "TEMP_REHEARSAL_WORKFLOW_RECORD_INVALID"
                    )
                self.workflow_guard.require(
                    workflow_record_id=record.workflow_record_id,
                    action="drop",
                    capability=self.capability,
                )
            return
        if database == self.capability.source_database:
            required = {
                "create": "source_create_started",
                "commit_identity": "source_identity_commit_started",
            }.get(action)
            if record.phase != required:
                raise TempPostgresRehearsalError(
                    "TEMP_REHEARSAL_PHASE_INVALID"
                )
            return
        if (
            database == self.capability.replacement_database
            and record.phase == "replacement_bound"
            and record.workflow_record_id is not None
        ):
            self.workflow_guard.require(
                workflow_record_id=record.workflow_record_id,
                action=action,
                capability=self.capability,
            )
            return
        raise TempPostgresRehearsalError("TEMP_REHEARSAL_PHASE_INVALID")
