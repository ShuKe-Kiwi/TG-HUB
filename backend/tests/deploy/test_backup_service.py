from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.deploy import backup_service as module
from app.deploy.backup_service import (
    BackupService,
    BackupServiceError,
    PgToolRunner,
    SnapshotMetadata,
    ToolResult,
    build_libpq_env,
    parse_pg_connection_spec,
    parse_pg_major,
    required_catalog_objects_present,
    required_free_bytes,
)


def _catalog(*, omit: str | None = None) -> bytes:
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
    dump_id = 1
    for table in tables:
        if table != omit:
            lines.append(f"{dump_id}; 1259 1 TABLE public {table} owner")
            dump_id += 1
        if table != "alembic_version" and table != omit:
            lines.append(f"{dump_id}; 0 1 TABLE DATA public {table} owner")
            dump_id += 1
    return ("\n".join(lines) + "\n").encode()


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    async def run(
        self, argv: list[str | Path], *, env: dict[str, str], cwd: Path | None = None
    ) -> ToolResult:
        values = [str(value) for value in argv]
        self.calls.append((values, dict(env)))
        if "--version" in values:
            name = Path(values[0]).name
            return ToolResult(0, f"{name} (PostgreSQL) 16.4\n".encode(), b"")
        if "--list" in values:
            return ToolResult(0, _catalog(), b"")
        target = next(value.removeprefix("--file=") for value in values if value.startswith("--file="))
        Path(target).write_bytes(b"PGDMP\x01fake-custom-dump")
        return ToolResult(0, b"", b"")


def _settings(tmp_path: Path) -> Settings:
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(
        json.dumps(
            {
                "source_channels": [{"ref": "https://t.me/example", "enabled": True}],
                "watch_titles": [{"title": "百花杀", "enabled": True, "aliases": []}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return Settings(
        APP_ENV="production",
        DATABASE_URL="postgresql+asyncpg://backup:secret@127.0.0.1:5432/tg_hub",
        BACKUP_DIR=tmp_path / "backups",
        WATCHLIST_PATH=watchlist,
    )


def test_connection_spec_and_libpq_env_are_allowlisted() -> None:
    spec = parse_pg_connection_spec(
        "postgresql+asyncpg://user:p%40ss@[::1]:5433/tg_hub"
    )
    env = build_libpq_env(
        spec,
        parent_env={
            "PATH": "/untrusted",
            "LANG": "C.UTF-8",
            "PGSERVICE": "evil",
            "PGPASSFILE": "/tmp/evil",
            "PGOPTIONS": "-c search_path=evil",
        },
        path="/approved/bin",
    )

    assert spec.host == "::1"
    assert spec.password == "p@ss"
    assert env == {
        "PATH": "/approved/bin",
        "LANG": "C.UTF-8",
        "PGHOST": "::1",
        "PGPORT": "5433",
        "PGUSER": "user",
        "PGDATABASE": "tg_hub",
        "PGPASSWORD": "p@ss",
    }


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///tmp.db",
        "postgresql+asyncpg:///tg_hub",
        "postgresql+asyncpg://localhost/tg_hub",
        "postgresql+asyncpg://user@localhost/",
        "postgresql+asyncpg://user@localhost/tg_hub?sslmode=require",
        "postgresql+asyncpg://user@%2Ftmp/tg_hub",
    ],
)
def test_connection_spec_rejects_implicit_or_unsupported_values(url: str) -> None:
    with pytest.raises(BackupServiceError, match="DATABASE_URL_UNSUPPORTED"):
        parse_pg_connection_spec(url)


def test_version_space_and_catalog_contracts_are_exact() -> None:
    assert parse_pg_major(b"pg_dump (PostgreSQL) 16.4\n") == 16
    assert required_free_bytes(100) == 256 * 1024 * 1024 + 100
    assert required_catalog_objects_present(_catalog()) is True
    assert required_catalog_objects_present(_catalog(omit="channels")) is False
    similar = _catalog().replace(b" channels ", b" channels_archive ")
    assert required_catalog_objects_present(similar) is False


async def test_pg_tool_runner_bounds_output_and_timeout() -> None:
    output_runner = PgToolRunner(timeout_seconds=2, output_limit_bytes=32)
    with pytest.raises(BackupServiceError, match="PG_TOOL_OUTPUT_LIMIT_EXCEEDED"):
        await output_runner.run(
            [sys.executable, "-c", "print('x' * 1000)"], env=os.environ
        )

    timeout_runner = PgToolRunner(timeout_seconds=0.03)
    with pytest.raises(BackupServiceError, match="PG_TOOL_TIMEOUT"):
        await timeout_runner.run(
            [sys.executable, "-c", "import time; time.sleep(10)"], env=os.environ
        )


async def test_create_builds_final_package_with_exported_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    (repository / "backend").mkdir(parents=True)
    (repository / "backend" / "uv.lock").write_bytes(b"locked")
    runner = FakeRunner()

    @asynccontextmanager
    async def fake_snapshot(database_url: str):
        assert "secret" in database_url
        yield SnapshotMetadata(
            snapshot_id="00000003-0000001B-1",
            source_server_version="16.4",
            source_server_major=16,
            database_size_bytes=1024,
            alembic_revision="head_revision",
        )

    async def clean(root: Path) -> bool:
        return root == repository

    monkeypatch.setattr(module, "exported_snapshot", fake_snapshot)
    monkeypatch.setattr(module, "_git_clean", clean)
    monkeypatch.setattr(module, "_git_output", lambda *_: "a" * 40)
    monkeypatch.setattr(module, "_expected_alembic_head", lambda *_: "head_revision")
    monkeypatch.setattr(module, "_resolve_tool", lambda name: Path(f"/tools/{name}"))
    monkeypatch.setattr(
        module.shutil,
        "disk_usage",
        lambda *_: SimpleNamespace(total=10**12, used=0, free=10**12),
    )
    settings = _settings(tmp_path)

    result = await BackupService(
        settings,
        repository_root=repository,
        runner=runner,
        allow_test_paths=True,
    ).create()

    assert result.status == "pass"
    assert result.backup_id is not None
    package = settings.BACKUP_DIR / result.backup_id
    assert {item.name for item in package.iterdir()} == {
        "database.dump",
        "watchlist.json",
        "manifest.json",
    }
    manifest = json.loads((package / "manifest.json").read_text())
    assert manifest["alembic_revision"] == "head_revision"
    assert manifest["exclusions"]["production_env"] == "secret_material_excluded"
    dump_call = next(call for call, _ in runner.calls if "--format=custom" in call)
    assert "--snapshot=00000003-0000001B-1" in dump_call
    assert not any("secret" in value for value in dump_call)
    dump_env = next(env for call, env in runner.calls if "--format=custom" in call)
    assert dump_env["PGPASSWORD"] == "secret"
    assert all(key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE", "SYSTEMROOT", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGPASSWORD"} for key in dump_env)


async def test_create_rejects_nonprivate_path_without_test_override(
    tmp_path: Path,
) -> None:
    result = await BackupService(_settings(tmp_path)).create()

    assert result.status == "fail"
    assert result.error_code == "BACKUP_PATH_INVALID"
    assert not (tmp_path / "backups").exists()
