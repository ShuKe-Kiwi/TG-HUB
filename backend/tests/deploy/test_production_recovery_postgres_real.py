from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.deploy.backup_models import PgConnectionSpec
from app.deploy.production_recovery_postgres import TempPostgresRehearsalError
from app.deploy.production_recovery_postgres_real import (
    DurableReplacementWorkflowGuard,
    FrozenObservationProvider,
    RealServerRoleObservationProvider,
    RealTempPostgresAdapter,
)
from app.deploy.production_recovery_postgres_record import (
    TempPostgresRehearsalRecordStore,
)


class FakeResult:
    def __init__(self, *, row=None, rows=()) -> None:
        self.row = row
        self.rows = tuple(rows)

    def one(self):
        return self.row

    def __iter__(self):
        return iter(self.rows)


class FakeServer:
    def __init__(
        self,
        *,
        superuser: bool = False,
        createdb: bool = True,
        delay_seconds: float = 0,
    ) -> None:
        self.superuser = superuser
        self.createdb = createdb
        self.delay_seconds = delay_seconds
        self.databases: dict[str, dict[str, object]] = {}
        self.executed: list[str] = []


class FakeConnection:
    def __init__(self, server: FakeServer, database: str) -> None:
        self.server = server
        self.database = database

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def scalar(self, statement, values=None):
        if self.server.delay_seconds:
            await asyncio.sleep(self.server.delay_seconds)
        sql = str(statement)
        if "current_database()" in sql:
            return self.database
        if "server_version_num" in sql:
            return "160004"
        if "pg_control_system" in sql:
            return "123456"
        values = values or {}
        database = values.get("database")
        if "CREATE DATABASE" in sql:
            self.server.databases[str(database)] = {
                "owner_oid": 1000,
                "comment": None,
                "active": 0,
                "prepared": 0,
            }
            return "SELECT 1"
        if "COMMENT ON DATABASE" in sql:
            self.server.databases[str(database)]["comment"] = values["comment"]
            return "SELECT 1"
        if "DROP DATABASE" in sql:
            self.server.databases.pop(str(database), None)
            return "SELECT 1"
        raise AssertionError(sql)

    async def execute(self, statement, values=None):
        sql = str(statement)
        self.server.executed.append(sql)
        if "FROM pg_roles" in sql:
            return FakeResult(
                row=SimpleNamespace(
                    oid=1000,
                    rolname="fixture",
                    rolcreatedb=self.server.createdb,
                    rolsuper=self.server.superuser,
                )
            )
        if "LEFT JOIN pg_database" in sql:
            database = str((values or {})["database"])
            state = self.server.databases.get(database)
            return FakeResult(
                row=SimpleNamespace(
                    exists=state is not None,
                    owner_oid=state and state["owner_oid"],
                    comment=state and state["comment"],
                    active_connections=state and state["active"] or 0,
                    prepared_transactions=state and state["prepared"] or 0,
                )
            )
        if "object_type" in sql:
            return FakeResult(
                rows=(
                    SimpleNamespace(
                        object_type="schema",
                        schema_name="public",
                        object_name="public",
                        user_owned=True,
                    ),
                )
            )
        return FakeResult()


class FakeEngine:
    def __init__(self, server: FakeServer, database: str) -> None:
        self.server = server
        self.database = database
        self.disposed = False

    def connect(self):
        return FakeConnection(self.server, self.database)

    async def dispose(self):
        self.disposed = True


class FakeEngineFactory:
    def __init__(self, server: FakeServer) -> None:
        self.server = server
        self.engines: list[FakeEngine] = []

    def __call__(self, url, **_kwargs):
        engine = FakeEngine(self.server, url.database)
        self.engines.append(engine)
        return engine


class FakeWorkflowGuard:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def require(self, *, workflow_record_id, action, capability) -> None:
        assert workflow_record_id == "f" * 32
        assert capability.replacement_database.startswith(
            "tg_hub_4c_replacement_"
        )
        self.calls.append((workflow_record_id, action))


class FakeProductionStore:
    def __init__(self, main, child=None) -> None:
        self.main = main
        self.child = child

    def read(self, record_id):
        assert record_id == "f" * 32
        return self.main

    def read_cleanup(self, main):
        assert main is self.main
        return self.child


def spec() -> PgConnectionSpec:
    return PgConnectionSpec(
        host="127.0.0.1",
        port=5432,
        user="fixture",
        database="postgres",
    )


async def test_real_provider_issues_capability_without_settings(tmp_path) -> None:
    factory = FakeEngineFactory(FakeServer())
    provider = RealServerRoleObservationProvider(engine_factory=factory)
    capability = await provider.issue_initial(root=tmp_path / "runtime", spec=spec())
    assert capability.role_oid == 1000
    assert capability.scope == "initial"
    assert all(engine.disposed for engine in factory.engines)


@pytest.mark.parametrize(
    ("createdb", "superuser"), [(False, False), (True, True)]
)
async def test_real_provider_rejects_unsafe_role(
    tmp_path, createdb, superuser
) -> None:
    factory = FakeEngineFactory(
        FakeServer(createdb=createdb, superuser=superuser)
    )
    provider = RealServerRoleObservationProvider(engine_factory=factory)
    with pytest.raises(TempPostgresRehearsalError, match="CONNECTION_UNSAFE"):
        await provider.issue_initial(root=tmp_path / "runtime", spec=spec())


async def test_real_provider_has_overall_timeout(tmp_path) -> None:
    factory = FakeEngineFactory(FakeServer(delay_seconds=0.05))
    provider = RealServerRoleObservationProvider(
        engine_factory=factory, overall_timeout_seconds=0.01
    )
    with pytest.raises(TempPostgresRehearsalError, match="PREFLIGHT_TIMEOUT"):
        await provider.issue_initial(root=tmp_path / "runtime", spec=spec())
    assert all(engine.disposed for engine in factory.engines)


async def test_real_adapter_create_and_identity_commit_are_generated_only(
    tmp_path,
) -> None:
    server = FakeServer()
    factory = FakeEngineFactory(server)
    provider = RealServerRoleObservationProvider(engine_factory=factory)
    capability = await provider.issue_initial(root=tmp_path / "runtime", spec=spec())
    store = TempPostgresRehearsalRecordStore(capability)
    record = store.advance(store.create(), "source_create_started")
    adapter = RealTempPostgresAdapter(
        capability=capability,
        spec=spec(),
        provider=provider,
        record_store=store,
        workflow_guard=FakeWorkflowGuard(),
        engine_factory=factory,
    )
    try:
        await adapter.create(capability.source_database)
        record = store.advance(record, "source_created")
        store.advance(record, "source_identity_commit_started")
        await adapter.commit_identity(capability.source_database)
        state = await adapter.inspect(capability.source_database)
        assert state.exists is True
        assert state.owner_oid == capability.role_oid
        assert state.comment is not None and capability.source_token in state.comment
        assert state.catalog_state == "empty"
        with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
            await adapter.create("tg_hub")
        with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
            await adapter.drop(capability.source_database)
    finally:
        await adapter.aclose()


async def test_live_identity_change_blocks_destructive_action(tmp_path) -> None:
    server = FakeServer()
    factory = FakeEngineFactory(server)
    provider = RealServerRoleObservationProvider(engine_factory=factory)
    capability = await provider.issue_initial(root=tmp_path / "runtime", spec=spec())
    store = TempPostgresRehearsalRecordStore(capability)
    store.advance(store.create(), "source_create_started")
    adapter = RealTempPostgresAdapter(
        capability=capability,
        spec=spec(),
        provider=provider,
        record_store=store,
        workflow_guard=FakeWorkflowGuard(),
        engine_factory=factory,
    )
    server.superuser = True
    try:
        with pytest.raises(TempPostgresRehearsalError, match="CONNECTION_UNSAFE"):
            await adapter.create(capability.source_database)
    finally:
        await adapter.aclose()


async def test_real_drop_uses_record_bound_cleanup_target(tmp_path) -> None:
    server = FakeServer()
    factory = FakeEngineFactory(server)
    provider = RealServerRoleObservationProvider(engine_factory=factory)
    capability = await provider.issue_initial(root=tmp_path / "runtime", spec=spec())
    store = TempPostgresRehearsalRecordStore(capability)
    record = store.create()
    for phase in (
        "source_create_started",
        "source_created",
        "source_identity_commit_started",
        "source_identity_committed",
        "dump_started",
        "dump_committed",
    ):
        record = store.advance(record, phase)
    record = store.update_facts(record, workflow_record_id="f" * 32)
    record = store.advance(record, "replacement_bound")
    workflow_guard = FakeWorkflowGuard()
    initial_adapter = RealTempPostgresAdapter(
        capability=capability,
        spec=spec(),
        provider=provider,
        record_store=store,
        workflow_guard=workflow_guard,
        engine_factory=factory,
    )
    await initial_adapter.create(capability.replacement_database)
    await initial_adapter.commit_identity(capability.replacement_database)
    await initial_adapter.aclose()
    assert [action for _, action in workflow_guard.calls] == [
        "create",
        "commit_identity",
    ]

    record = store.advance(record, "workflow_terminal")
    observation = await provider.observe(spec())
    resume = store.issue_resume_cleanup(
        spec=spec(), provider=FrozenObservationProvider(observation)
    )
    cleanup_guard = FakeWorkflowGuard()
    cleanup_adapter = RealTempPostgresAdapter(
        capability=resume,
        spec=spec(),
        provider=provider,
        record_store=store,
        workflow_guard=cleanup_guard,
        engine_factory=factory,
    )
    try:
        await cleanup_adapter.drop(capability.replacement_database)
        assert capability.replacement_database not in server.databases
        assert [action for _, action in cleanup_guard.calls] == ["drop"]
        with pytest.raises(TempPostgresRehearsalError, match="NOT_AUTHORIZED"):
            await cleanup_adapter.drop(capability.source_database)
    finally:
        await cleanup_adapter.aclose()


def test_durable_workflow_guard_checks_main_and_child_phase(tmp_path) -> None:
    factory = FakeEngineFactory(FakeServer())
    provider = RealServerRoleObservationProvider(engine_factory=factory)

    async def build_capability():
        return await provider.issue_initial(root=tmp_path / "runtime", spec=spec())

    import asyncio

    capability = asyncio.run(build_capability())
    resources = SimpleNamespace(
        replacement_database_identity=capability.replacement_database,
        replacement_identity_token=capability.replacement_token,
    )
    main = SimpleNamespace(
        phase="replacement_create_started",
        resources=resources,
        cleanup_requested="no",
    )
    guard = DurableReplacementWorkflowGuard(FakeProductionStore(main))
    guard.require(
        workflow_record_id="f" * 32,
        action="create",
        capability=capability,
    )
    main.phase = "rolled_back"
    main.cleanup_requested = "yes"
    child = SimpleNamespace(phase="cleanup_started", drop_observed="no")
    guard = DurableReplacementWorkflowGuard(FakeProductionStore(main, child))
    guard.require(
        workflow_record_id="f" * 32,
        action="drop",
        capability=capability,
    )
    child.phase = "planned"
    with pytest.raises(TempPostgresRehearsalError, match="WORKFLOW_PHASE_INVALID"):
        guard.require(
            workflow_record_id="f" * 32,
            action="drop",
            capability=capability,
        )
