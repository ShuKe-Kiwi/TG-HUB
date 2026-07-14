"""Guarded PostgreSQL operations for isolated restore verification."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import ForeignKeyConstraint, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy import make_url, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.deploy.backup_models import PgConnectionSpec, validate_restore_target_name

IDENTITY_PREFIX = "tg-hub-restore-verify:"
REQUIRED_TABLES = frozenset(
    {
        "alembic_version",
        "channels",
        "raw_messages",
        "works",
        "resources",
        "resource_links",
        "resource_sources",
    }
)


class RestoreDatabaseError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class TargetState:
    exists: bool
    owner_matches: bool
    comment: str | None
    active_connections: int
    prepared_transactions: int


@dataclass(frozen=True)
class VerificationSummary:
    schema_verified: bool
    constraints_verified: bool
    integrity_verified: bool


def database_url_for(database_url: str, database: str) -> str:
    return make_url(database_url).set(database=database).render_as_string(
        hide_password=False
    )


class RestoreDatabaseAdapter:
    def __init__(
        self,
        database_url: str,
        spec: PgConnectionSpec,
        *,
        maintenance_database: str = "postgres",
    ) -> None:
        self.database_url = database_url
        self.spec = spec
        self.maintenance_database = maintenance_database
        self.engine = create_async_engine(
            database_url_for(database_url, maintenance_database),
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
        )

    async def server_major(self) -> int:
        try:
            async with self.engine.connect() as connection:
                value = await connection.scalar(text("SHOW server_version_num"))
            return int(str(value)) // 10000
        except Exception as exc:
            raise RestoreDatabaseError("RESTORE_MAINTENANCE_UNAVAILABLE") from exc

    async def create_target(self, target: str) -> None:
        self._guard_name(target)
        await self._formatted_ddl(
            "SELECT format('CREATE DATABASE %I OWNER %I', "
            "CAST(:target AS text), CAST(:owner AS text))",
            {"target": target, "owner": self.spec.user},
            "RESTORE_CREATE_FAILED",
        )

    async def commit_identity(self, target: str, token: str) -> None:
        self._guard_name(target)
        await self._formatted_ddl(
            "SELECT format('COMMENT ON DATABASE %I IS %L', "
            "CAST(:target AS text), CAST(:comment AS text))",
            {"target": target, "comment": f"{IDENTITY_PREFIX}{token}"},
            "RESTORE_TARGET_IDENTITY_UNCOMMITTED",
        )

    async def inspect_target(self, target: str) -> TargetState:
        self._guard_name(target)
        query = text(
            """
            SELECT d.datname IS NOT NULL AS exists,
                   COALESCE(r.rolname = :owner, false) AS owner_matches,
                   shobj_description(d.oid, 'pg_database') AS comment,
                   COALESCE((SELECT count(*) FROM pg_stat_activity a
                             WHERE a.datname = d.datname
                               AND a.pid <> pg_backend_pid()), 0) AS active_connections,
                   COALESCE((SELECT count(*) FROM pg_prepared_xacts p
                             WHERE p.database = d.datname), 0) AS prepared_transactions
              FROM (SELECT CAST(:target AS text) AS requested) q
              LEFT JOIN pg_database d ON d.datname = q.requested
              LEFT JOIN pg_roles r ON r.oid = d.datdba
            """
        )
        try:
            async with self.engine.connect() as connection:
                row = (
                    await connection.execute(
                        query, {"target": target, "owner": self.spec.user}
                    )
                ).one()
            return TargetState(
                exists=bool(row.exists),
                owner_matches=bool(row.owner_matches),
                comment=row.comment,
                active_connections=int(row.active_connections),
                prepared_transactions=int(row.prepared_transactions),
            )
        except Exception as exc:
            raise RestoreDatabaseError("RESTORE_MAINTENANCE_UNAVAILABLE") from exc

    async def current_database_is(self, target: str) -> bool:
        self._guard_name(target)
        engine = create_async_engine(
            database_url_for(self.database_url, target), pool_pre_ping=True
        )
        try:
            async with engine.connect() as connection:
                return await connection.scalar(text("SELECT current_database()")) == target
        except Exception as exc:
            raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH") from exc
        finally:
            await engine.dispose()

    async def drop_target(self, target: str) -> None:
        self._guard_name(target)
        await self._formatted_ddl(
            "SELECT format('DROP DATABASE %I', CAST(:target AS text))",
            {"target": target},
            "RESTORE_DROP_FAILED",
        )

    async def aclose(self) -> None:
        await self.engine.dispose()

    async def _formatted_ddl(
        self, formatter: str, values: dict[str, str], error_code: str
    ) -> None:
        try:
            async with self.engine.connect() as connection:
                statement = await connection.scalar(text(formatter), values)
                if not isinstance(statement, str):
                    raise RestoreDatabaseError(error_code)
                await connection.execute(text(statement))
        except RestoreDatabaseError:
            raise
        except Exception as exc:
            raise RestoreDatabaseError(error_code) from exc

    def _guard_name(self, target: str) -> None:
        try:
            validate_restore_target_name(target)
        except ValueError as exc:
            raise RestoreDatabaseError("RESTORE_TARGET_UNSAFE") from exc
        if target in {self.spec.database, self.maintenance_database}:
            raise RestoreDatabaseError("RESTORE_TARGET_UNSAFE")


class RestoreDatabaseVerifier:
    def __init__(self, database_url: str, *, statement_timeout_seconds: int = 30) -> None:
        self.database_url = database_url
        self.statement_timeout_seconds = statement_timeout_seconds

    async def verify(
        self, target: str, *, expected_revision: str
    ) -> VerificationSummary:
        validate_restore_target_name(target)
        engine = create_async_engine(database_url_for(self.database_url, target))
        connection: AsyncConnection | None = None
        try:
            connection = await engine.connect()
            await connection.execute(text("BEGIN READ ONLY"))
            await connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{self.statement_timeout_seconds}s"},
            )
            readonly = await connection.scalar(text("SHOW transaction_read_only"))
            if readonly != "on":
                raise RestoreDatabaseError("RESTORE_READONLY_SMOKE_FAILED")
            if await connection.scalar(text("SELECT current_database()")) != target:
                raise RestoreDatabaseError("RESTORE_TARGET_IDENTITY_MISMATCH")
            await self._verify_schema(connection, expected_revision)
            await self._verify_constraints(connection)
            await self._verify_integrity(connection)
            return VerificationSummary(True, True, True)
        except RestoreDatabaseError:
            raise
        except Exception as exc:
            raise RestoreDatabaseError("RESTORE_INTEGRITY_FAILED") from exc
        finally:
            if connection is not None:
                await connection.rollback()
                await connection.close()
            await engine.dispose()

    async def _verify_schema(
        self, connection: AsyncConnection, expected_revision: str
    ) -> None:
        table_rows = await connection.execute(
            text(
                """
                SELECT c.relname
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public' AND c.relkind = 'r'
                """
            )
        )
        tables = {str(row[0]) for row in table_rows}
        revisions = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalars().all()
        if not REQUIRED_TABLES.issubset(tables) or revisions != [expected_revision]:
            raise RestoreDatabaseError("RESTORE_SCHEMA_MISMATCH")
        column_rows = await connection.execute(
            text(
                """
                SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod),
                       a.attnotnull
                  FROM pg_attribute a
                  JOIN pg_class c ON c.oid = a.attrelid
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'public'
                   AND c.relkind = 'r'
                   AND a.attnum > 0
                   AND NOT a.attisdropped
                """
            )
        )
        actual = {
            (str(row[0]), str(row[1])): (str(row[2]).lower(), bool(row[3]))
            for row in column_rows
        }
        for key, expected in _expected_columns().items():
            if actual.get(key) != expected:
                raise RestoreDatabaseError("RESTORE_SCHEMA_MISMATCH")

    async def _verify_constraints(self, connection: AsyncConnection) -> None:
        rows = await connection.execute(
            text(
                """
                SELECT c.contype, src.relname,
                       ARRAY(
                           SELECT a.attname
                             FROM unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
                             JOIN pg_attribute a
                               ON a.attrelid = c.conrelid AND a.attnum = k.attnum
                            ORDER BY k.ord
                       ) AS source_columns,
                       dst.relname AS target_table,
                       CASE WHEN c.contype = 'f' THEN ARRAY(
                           SELECT a.attname
                             FROM unnest(c.confkey) WITH ORDINALITY AS k(attnum, ord)
                             JOIN pg_attribute a
                               ON a.attrelid = c.confrelid AND a.attnum = k.attnum
                            ORDER BY k.ord
                       ) ELSE ARRAY[]::name[] END AS target_columns,
                       c.confdeltype, c.confupdtype, c.convalidated
                  FROM pg_constraint c
                  JOIN pg_class src ON src.oid = c.conrelid
                  JOIN pg_namespace src_n ON src_n.oid = src.relnamespace
                  LEFT JOIN pg_class dst ON dst.oid = c.confrelid
                 WHERE src_n.nspname = 'public'
                   AND src.relname = ANY(CAST(:tables AS text[]))
                   AND c.contype IN ('p', 'u', 'f')
                """
            ),
            {"tables": sorted(REQUIRED_TABLES - {"alembic_version"})},
        )
        actual = {
            (
                _pg_char(row.contype),
                str(row.relname),
                tuple(row.source_columns),
                str(row.target_table) if row.target_table is not None else None,
                tuple(row.target_columns),
                _pg_char(row.confdeltype),
                _pg_char(row.confupdtype),
            )
            for row in rows
            if row.convalidated
        }
        if not _expected_constraints().issubset(actual):
            raise RestoreDatabaseError("RESTORE_CONSTRAINT_MISMATCH")

    async def _verify_integrity(self, connection: AsyncConnection) -> None:
        for table in sorted(REQUIRED_TABLES - {"alembic_version"}):
            # Names come exclusively from the fixed allowlist above.
            await connection.scalar(text(f'SELECT count(*) FROM public."{table}"'))
        orphan_count = await connection.scalar(
            text(
                """
                SELECT count(*)
                  FROM raw_messages rm
                  LEFT JOIN channels c ON c.id = rm.channel_id
                 WHERE c.id IS NULL
                """
            )
        )
        if orphan_count:
            raise RestoreDatabaseError("RESTORE_INTEGRITY_FAILED")


def _expected_columns() -> dict[tuple[str, str], tuple[str, bool]]:
    # Importing model modules registers every locked core table on Base.metadata.
    from app.database import Base
    from app.modules.channel import model as _channel_model  # noqa: F401
    from app.modules.rawmessage import model as _rawmessage_model  # noqa: F401
    from app.modules.resource import model as _resource_model  # noqa: F401

    expected: dict[tuple[str, str], tuple[str, bool]] = {}
    dialect = postgresql.dialect()
    for table_name in REQUIRED_TABLES - {"alembic_version"}:
        table = Base.metadata.tables[table_name]
        for column in table.columns:
            compiled = str(column.type.compile(dialect=dialect)).lower()
            expected[(table_name, column.name)] = (
                _catalog_type_name(compiled),
                not column.nullable,
            )
    expected[("alembic_version", "version_num")] = (
        "character varying(32)",
        True,
    )
    return expected


def _catalog_type_name(compiled: str) -> str:
    if compiled.startswith("varchar("):
        return compiled.replace("varchar", "character varying", 1)
    return {
        "float": "double precision",
        "timestamp with time zone": "timestamp with time zone",
    }.get(compiled, compiled)


def _pg_char(value: str | bytes) -> str:
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="strict")
    if len(value) != 1:
        raise RestoreDatabaseError("RESTORE_CONSTRAINT_MISMATCH")
    return value


def _expected_constraints() -> set[
    tuple[str, str, tuple[str, ...], str | None, tuple[str, ...], str, str]
]:
    from app.database import Base
    from app.modules.channel import model as _channel_model  # noqa: F401
    from app.modules.rawmessage import model as _rawmessage_model  # noqa: F401
    from app.modules.resource import model as _resource_model  # noqa: F401

    expected = set()
    action_codes = {
        None: "a",
        "NO ACTION": "a",
        "RESTRICT": "r",
        "CASCADE": "c",
        "SET NULL": "n",
        "SET DEFAULT": "d",
    }
    for table_name in REQUIRED_TABLES - {"alembic_version"}:
        table = Base.metadata.tables[table_name]
        for constraint in table.constraints:
            columns = tuple(column.name for column in constraint.columns)
            if isinstance(constraint, PrimaryKeyConstraint):
                expected.add(("p", table_name, columns, None, (), " ", " "))
            elif isinstance(constraint, UniqueConstraint):
                expected.add(("u", table_name, columns, None, (), " ", " "))
            elif isinstance(constraint, ForeignKeyConstraint):
                elements = tuple(constraint.elements)
                target_table = elements[0].column.table.name
                target_columns = tuple(element.column.name for element in elements)
                expected.add(
                    (
                        "f",
                        table_name,
                        columns,
                        target_table,
                        target_columns,
                        action_codes[constraint.ondelete],
                        action_codes[constraint.onupdate],
                    )
                )
    return expected
