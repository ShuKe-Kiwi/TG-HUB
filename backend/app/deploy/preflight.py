"""Shared, desensitized production preflight and readiness checks."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.modules.monitor.config import load_watchlist

CheckStatus = Literal["pass", "fail"]

EXIT_CONFIG = 10
EXIT_PERMISSION = 11
EXIT_PORT = 12
EXIT_DATABASE = 13
EXIT_MIGRATION = 14
EXIT_WATCHLIST = 15
EXIT_STATIC = 16


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


class StartupPreflightReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["pass", "fail"]
    error_code: str | None = None
    exit_code: int = 0
    checks: dict[str, CheckStatus]
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


def _port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) != 0


async def run_startup_preflight(
    env_file: Path,
    *,
    check_port: bool = True,
    home: Path | None = None,
) -> StartupPreflightReport:
    """Fail-closed production startup validation with desensitized output."""
    checks: dict[str, CheckStatus] = {}
    env_file = env_file.expanduser().resolve()
    checks["env_file"] = "pass" if env_file.is_file() else "fail"
    if checks["env_file"] == "fail":
        return StartupPreflightReport(status="fail", error_code="ENV_FILE_MISSING", exit_code=EXIT_CONFIG, checks=checks)
    mode = stat.S_IMODE(env_file.stat().st_mode)
    checks["env_permissions"] = "pass" if mode & 0o077 == 0 else "fail"
    if checks["env_permissions"] == "fail":
        return StartupPreflightReport(status="fail", error_code="ENV_FILE_PERMISSION", exit_code=EXIT_PERMISSION, checks=checks)
    try:
        from app.config import load_settings

        configured = load_settings(env_file)
    except Exception:
        checks["config"] = "fail"
        return StartupPreflightReport(status="fail", error_code="CONFIG_INVALID", exit_code=EXIT_CONFIG, checks=checks)
    checks["config"] = "pass"
    contract = validate_production_config(configured, home=home)
    production_ok = configured.APP_ENV == "production"
    port_valid = 1 <= configured.ADMIN_PORT <= 65535
    checks.update(
        production_env="pass" if production_ok else "fail",
        config_contract=contract.status,
        port_range="pass" if port_valid else "fail",
    )
    if not production_ok or contract.status == "fail" or not port_valid:
        return StartupPreflightReport(status="fail", error_code="CONFIG_INVALID", exit_code=EXIT_CONFIG, checks=checks)
    port_ok = not check_port or _port_available(configured.ADMIN_BIND_HOST, configured.ADMIN_PORT)
    checks["port_available"] = "pass" if port_ok else "fail"
    if not port_ok:
        return StartupPreflightReport(status="fail", error_code="PORT_OCCUPIED", exit_code=EXIT_PORT, checks=checks)
    try:
        load_watchlist(configured.WATCHLIST_PATH)
        checks["watchlist"] = "pass"
    except Exception:
        checks["watchlist"] = "fail"
        return StartupPreflightReport(status="fail", error_code="WATCHLIST_INVALID", exit_code=EXIT_WATCHLIST, checks=checks)
    engine = create_async_engine(configured.DATABASE_URL, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        report = await check_application_readiness(
            configured,
            session_factory=factory,
            assembly_ready=True,
            timeout_seconds=2.0,
        )
    finally:
        await engine.dispose()
    checks["database"] = report.checks.database
    checks["migration"] = report.checks.migration
    if report.checks.database == "fail":
        return StartupPreflightReport(status="fail", error_code=report.error_code or "DATABASE_UNAVAILABLE", exit_code=EXIT_DATABASE, checks=checks)
    if report.checks.migration == "fail":
        return StartupPreflightReport(status="fail", error_code="MIGRATION_NOT_AT_HEAD", exit_code=EXIT_MIGRATION, checks=checks)
    return StartupPreflightReport(status="pass", checks=checks)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "startup":
        print(json.dumps({"status": "fail", "error_code": "USAGE_INVALID", "report_desensitized": "yes"}))
        return EXIT_STATIC
    selected = os.environ.get("TG_HUB_ENV_FILE")
    if not selected:
        print(json.dumps({"status": "fail", "error_code": "ENV_FILE_NOT_CONFIGURED", "report_desensitized": "yes"}))
        return EXIT_CONFIG
    report = asyncio.run(run_startup_preflight(Path(selected)))
    print(report.model_dump_json())
    return report.exit_code


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


if __name__ == "__main__":
    raise SystemExit(main())
