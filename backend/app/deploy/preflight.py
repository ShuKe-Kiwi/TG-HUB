"""Shared, desensitized production preflight and readiness checks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.config import Settings
from app.modules.monitor.config import load_watchlist

CheckStatus = Literal["pass", "fail"]


class ProductionConfigReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["pass", "fail"]
    config_schema: CheckStatus
    bind_host: CheckStatus
    private_paths: CheckStatus
    blockers: list[str]
    report_desensitized: Literal["yes"] = "yes"


class ReadinessChecks(BaseModel):
    model_config = ConfigDict(frozen=True)

    database: CheckStatus
    migration: CheckStatus
    watchlist: CheckStatus
    assembly: CheckStatus


class MonitorReadiness(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool
    state: str
    error_code: str | None = None


class ReadinessReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["ready", "not_ready"]
    checks: ReadinessChecks
    monitor: MonitorReadiness
    error_code: str | None = None
    report_desensitized: Literal["yes"] = "yes"


def _inside_private_root(path: str | Path, private_root: Path) -> bool:
    try:
        Path(path).expanduser().resolve().relative_to(private_root)
    except (OSError, ValueError):
        return False
    return True


def validate_production_config(
    settings: Settings,
    *,
    home: Path | None = None,
) -> ProductionConfigReport:
    """Validate the production-only contract without exposing values."""
    if settings.APP_ENV != "production":
        return ProductionConfigReport(
            status="pass",
            config_schema="pass",
            bind_host="pass",
            private_paths="pass",
            blockers=[],
        )

    private_root = (home or Path.home()).expanduser().resolve() / ".tg-hub"
    schema_ok = settings.CONFIG_SCHEMA_VERSION == 1
    host_ok = settings.ADMIN_BIND_HOST == "127.0.0.1"
    paths_ok = all(
        _inside_private_root(value, private_root)
        for value in (
            settings.WATCHLIST_PATH,
            settings.TELEGRAM_SESSION_NAME,
            settings.HEARTBEAT_PATH,
            settings.LOG_DIR,
            settings.BACKUP_DIR,
        )
    )
    blockers: list[str] = []
    if not schema_ok:
        blockers.append("CONFIG_SCHEMA_UNSUPPORTED")
    if not host_ok:
        blockers.append("ADMIN_BIND_NOT_LOOPBACK")
    if not paths_ok:
        blockers.append("PRIVATE_PATH_OUTSIDE_ROOT")
    return ProductionConfigReport(
        status="pass" if not blockers else "fail",
        config_schema="pass" if schema_ok else "fail",
        bind_host="pass" if host_ok else "fail",
        private_paths="pass" if paths_ok else "fail",
        blockers=blockers,
    )


def _expected_alembic_heads(alembic_ini: Path) -> set[str]:
    config = Config(str(alembic_ini))
    return set(ScriptDirectory.from_config(config).get_heads())


async def check_application_readiness(
    settings: Settings,
    *,
    session_factory: Callable[[], Any],
    assembly_ready: bool,
    monitor_state: str = "stopped",
    monitor_error_code: str | None = None,
    alembic_ini: Path | None = None,
    timeout_seconds: float = 2.0,
) -> ReadinessReport:
    """Check application readiness; Monitor state is observational only."""
    database_ok = False
    migration_ok = False
    database_error: str | None = None
    expected_heads: set[str] = set()
    try:
        ini = alembic_ini or Path(__file__).parents[2] / "alembic.ini"
        expected_heads = _expected_alembic_heads(ini)
        async with session_factory() as session:
            await asyncio.wait_for(session.execute(text("SELECT 1")), timeout_seconds)
            database_ok = True
            result = await asyncio.wait_for(
                session.execute(text("SELECT version_num FROM alembic_version")),
                timeout_seconds,
            )
            current = {str(value) for value in result.scalars().all()}
            migration_ok = bool(expected_heads) and current == expected_heads
    except asyncio.TimeoutError:
        database_error = "DATABASE_TIMEOUT"
    except Exception:
        database_error = "DATABASE_UNAVAILABLE"

    try:
        load_watchlist(settings.WATCHLIST_PATH)
        watchlist_ok = True
    except Exception:
        watchlist_ok = False

    checks = ReadinessChecks(
        database="pass" if database_ok else "fail",
        migration="pass" if migration_ok else "fail",
        watchlist="pass" if watchlist_ok else "fail",
        assembly="pass" if assembly_ready else "fail",
    )
    error_code = database_error
    if database_ok and not migration_ok:
        error_code = "MIGRATION_NOT_AT_HEAD"
    elif database_ok and migration_ok and not watchlist_ok:
        error_code = "WATCHLIST_INVALID"
    elif database_ok and migration_ok and watchlist_ok and not assembly_ready:
        error_code = "ASSEMBLY_NOT_READY"

    ready = all(value == "pass" for value in checks.model_dump().values())
    return ReadinessReport(
        status="ready" if ready else "not_ready",
        checks=checks,
        monitor=MonitorReadiness(
            enabled=settings.MONITOR_AUTO_START,
            state=monitor_state,
            error_code=monitor_error_code,
        ),
        error_code=None if ready else error_code,
    )
