from __future__ import annotations

import inspect
import getpass
import os
from datetime import datetime, timezone

import pytest

from app.deploy.backup_models import PgConnectionSpec, generate_restore_identity
from app.deploy.restore_database import (
    IDENTITY_PREFIX,
    RestoreDatabaseAdapter,
    RestoreDatabaseError,
    RestoreDatabaseVerifier,
    _expected_columns,
    _expected_constraints,
    database_url_for,
)


def test_database_url_replacement_preserves_explicit_connection_identity() -> None:
    result = database_url_for(
        "postgresql+asyncpg://user:p%40ss@127.0.0.1:5433/tg_hub",
        "postgres",
    )

    assert result == "postgresql+asyncpg://user:p%40ss@127.0.0.1:5433/postgres"


def test_adapter_rejects_production_maintenance_and_unsafe_target() -> None:
    adapter = RestoreDatabaseAdapter(
        "postgresql+asyncpg://user:secret@127.0.0.1:5432/tg_hub",
        PgConnectionSpec(
            host="127.0.0.1", port=5432, user="user",
            database="tg_hub", password="secret",
        ),
    )

    for target in ("tg_hub", "postgres", "unsafe"):
        with pytest.raises(RestoreDatabaseError, match="RESTORE_TARGET_UNSAFE"):
            adapter._guard_name(target)


def test_constraint_verifier_uses_catalog_arrays_not_definition_text() -> None:
    source = inspect.getsource(RestoreDatabaseVerifier._verify_constraints)

    assert "pg_constraint" in source
    assert "pg_attribute" in source
    assert "conkey" in source
    assert "confkey" in source
    assert "pg_get_constraintdef" not in source


def test_expected_columns_follow_locked_orm_metadata() -> None:
    columns = _expected_columns()

    assert columns[("channels", "tg_id")] == ("bigint", True)
    assert columns[("raw_messages", "raw_payload")] == ("jsonb", False)
    assert columns[("works", "aliases")] == ("text[]", True)
    assert columns[("resources", "quality")] == ("character varying(50)", False)
    assert columns[("alembic_version", "version_num")] == (
        "character varying(32)",
        True,
    )


def test_expected_constraints_include_locked_pk_unique_and_fk_actions() -> None:
    constraints = _expected_constraints()

    assert ("p", "channels", ("id",), None, (), " ", " ") in constraints
    assert (
        "u", "raw_messages", ("channel_id", "tg_message_id"),
        None, (), " ", " ",
    ) in constraints
    assert (
        "f", "raw_messages", ("channel_id",), "channels", ("id",), "r", "a",
    ) in constraints


@pytest.mark.skipif(
    os.environ.get("RUN_RESTORE_ADAPTER_INTEGRATION") != "1",
    reason="requires an explicitly approved local PostgreSQL test instance",
)
async def test_real_adapter_create_identify_and_guarded_drop() -> None:
    user = getpass.getuser()
    url = f"postgresql+asyncpg://{user}@127.0.0.1:5432/tg_hub_test"
    adapter = RestoreDatabaseAdapter(
        url,
        PgConnectionSpec(
            host="127.0.0.1", port=5432, user=user, database="tg_hub_test"
        ),
    )
    target, _, token = generate_restore_identity(datetime.now(timezone.utc))
    try:
        await adapter.create_target(target)
        created = await adapter.inspect_target(target)
        assert created.exists and created.owner_matches and created.comment is None
        await adapter.commit_identity(target, token)
        identified = await adapter.inspect_target(target)
        assert identified.comment == f"{IDENTITY_PREFIX}{token}"
        assert await adapter.current_database_is(target)
        assert identified.active_connections == 0
        assert identified.prepared_transactions == 0
        await adapter.drop_target(target)
        assert not (await adapter.inspect_target(target)).exists
    finally:
        state = await adapter.inspect_target(target)
        if state.exists and not state.active_connections and not state.prepared_transactions:
            await adapter.drop_target(target)
        await adapter.aclose()


@pytest.mark.skipif(
    not os.environ.get("RESTORE_ADAPTER_CLEANUP_TARGET"),
    reason="requires an explicit test-only restore target",
)
async def test_guarded_cleanup_of_explicit_empty_test_target() -> None:
    target = os.environ["RESTORE_ADAPTER_CLEANUP_TARGET"]
    user = getpass.getuser()
    adapter = RestoreDatabaseAdapter(
        f"postgresql+asyncpg://{user}@127.0.0.1:5432/tg_hub_test",
        PgConnectionSpec(
            host="127.0.0.1", port=5432, user=user, database="tg_hub_test"
        ),
    )
    try:
        state = await adapter.inspect_target(target)
        if not state.exists:
            return
        assert state.exists and state.owner_matches
        assert state.comment is None or state.comment.startswith(IDENTITY_PREFIX)
        assert state.active_connections == 0
        assert state.prepared_transactions == 0
        await adapter.drop_target(target)
        assert not (await adapter.inspect_target(target)).exists
    finally:
        await adapter.aclose()
