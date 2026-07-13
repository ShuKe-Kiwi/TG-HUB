import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from app.config import Settings
from app.deploy import check_application_readiness
from app.main import create_app
from app.modules.monitor.control import MonitorStopResult
from app.modules.monitor.watchlist_service import WatchlistApplicationService

_HEAD = "c4a8e7b1d2f0"


class FakeResult:
    def __init__(self, values: list[str]) -> None:
        self.values = values

    def scalars(self):
        return self

    def all(self) -> list[str]:
        return self.values


class FakeSession:
    def __init__(self, head: str = _HEAD) -> None:
        self.head = head

    async def execute(self, statement):
        if "alembic_version" in str(statement):
            return FakeResult([self.head])
        return FakeResult([])


class SlowSession:
    async def execute(self, statement):
        await asyncio.sleep(1)
        return FakeResult([])


def _factory(session):
    @asynccontextmanager
    async def factory():
        yield session

    return factory


def _write_watchlist(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "source_channels": [{"ref": "-1001234567890"}],
                "watch_titles": [{"title": "家业"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class FakeControl:
    async def status(self):
        return type(
            "Snapshot",
            (),
            {"control_state": "failed", "last_error_code": "AUTO_START_FAILED"},
        )()

    async def shutdown(self):
        return MonitorStopResult(
            status="already_stopped",
            control_state="stopped",
            task_owned=False,
        )


async def test_readiness_passes_while_monitor_is_failed(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    _write_watchlist(watchlist)
    configured = Settings(WATCHLIST_PATH=watchlist, MONITOR_AUTO_START=True)

    report = await check_application_readiness(
        configured,
        session_factory=_factory(FakeSession()),
        assembly_ready=True,
        monitor_state="failed",
        monitor_error_code="AUTO_START_FAILED",
    )

    assert report.status == "ready"
    assert report.monitor.state == "failed"
    assert report.monitor.error_code == "AUTO_START_FAILED"


async def test_readiness_timeout_is_stable_and_desensitized(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    _write_watchlist(watchlist)
    configured = Settings(WATCHLIST_PATH=watchlist)

    report = await check_application_readiness(
        configured,
        session_factory=_factory(SlowSession()),
        assembly_ready=True,
        timeout_seconds=0.01,
    )

    assert report.status == "not_ready"
    assert report.error_code == "DATABASE_TIMEOUT"
    assert "postgresql" not in report.model_dump_json()


async def test_readiness_rejects_previous_migration_head(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    _write_watchlist(watchlist)

    report = await check_application_readiness(
        Settings(WATCHLIST_PATH=watchlist),
        session_factory=_factory(FakeSession("7b3f2a1c9d04")),
        assembly_ready=True,
    )

    assert report.status == "not_ready"
    assert report.checks.database == "pass"
    assert report.checks.migration == "fail"
    assert report.error_code == "MIGRATION_NOT_AT_HEAD"


async def test_health_endpoints_have_separate_semantics(tmp_path: Path) -> None:
    watchlist = tmp_path / "watchlist.json"
    _write_watchlist(watchlist)
    configured = Settings(WATCHLIST_PATH=watchlist, MONITOR_AUTO_START=True)
    application = create_app(
        app_settings=configured,
        session_factory=_factory(FakeSession()),
        watchlist_service=WatchlistApplicationService(path=watchlist),
        monitor_control_service=FakeControl(),
        admin_csrf_token="health-test",
    )
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://127.0.0.1",
        ) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")

    assert live.status_code == 200
    assert live.json() == {"status": "ok", "service": "tg-hub"}
    assert ready.status_code == 200
    assert ready.headers["cache-control"] == "no-store"
    assert ready.json()["status"] == "ready"
    assert ready.json()["monitor"]["state"] == "failed"
