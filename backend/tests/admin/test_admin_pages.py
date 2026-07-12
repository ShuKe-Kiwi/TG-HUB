import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from app.config import Settings
from app.main import create_app
from app.modules.monitor.control import MonitorStopResult
from app.modules.monitor.watchlist_service import WatchlistApplicationService

_CSRF = "page-test-csrf"


class PageControlService:
    def __init__(self) -> None:
        self.shutdown_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1
        return MonitorStopResult(
            status="already_stopped",
            control_state="stopped",
            task_owned=False,
        )


def _write_watchlist(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "source_channels": [
                    {"ref": "-1001234567890", "enabled": True}
                ],
                "watch_titles": [
                    {"title": "家业", "enabled": True, "aliases": []}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


@asynccontextmanager
async def _client(
    tmp_path: Path,
    *,
    remote: bool = False,
) -> AsyncIterator[tuple[httpx.AsyncClient, PageControlService]]:
    path = tmp_path / "watchlist.json"
    _write_watchlist(path)
    control = PageControlService()
    application = create_app(
        app_settings=Settings(
            APP_ENV="test",
            WATCHLIST_PATH=path,
            TELEGRAM_API_HASH="must-not-render",
            TELEGRAM_BOT_TOKEN="must-not-render",
            DATABASE_URL="postgresql+asyncpg://must-not-render/db",
        ),
        watchlist_service=WatchlistApplicationService(path=path),
        monitor_control_service=control,
        admin_csrf_token=_CSRF,
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(
            app=application,
            client=(("192.0.2.10", 43123) if remote else ("127.0.0.1", 43123)),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            yield client, control


async def test_overview_page_renders_local_shell_and_csrf_meta(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, control):
        response = await client.get("/admin/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert '<meta name="tg-hub-csrf" content="page-test-csrf">' in response.text
    assert "运行概览" in response.text
    assert 'id="start-monitor"' in response.text
    assert 'id="preflight-dialog"' in response.text
    assert 'id="preflight-result"' in response.text
    assert "must-not-render" not in response.text
    assert control.shutdown_calls == 1


async def test_watchlist_page_contains_both_config_tabs_and_editors(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _):
        response = await client.get("/admin/watchlist")

    assert response.status_code == 200
    assert 'id="channels-tab"' in response.text
    assert 'id="titles-tab"' in response.text
    assert 'id="channel-dialog"' in response.text
    assert 'id="title-dialog"' in response.text
    assert 'id="save-watchlist"' in response.text


async def test_admin_root_redirects_to_overview(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _):
        response = await client.get("/admin", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/admin/"


async def test_admin_static_assets_are_served_without_cache(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _):
        css = await client.get("/admin/static/admin.css")
        javascript = await client.get("/admin/static/admin.js")

    assert css.status_code == 200
    assert css.headers["cache-control"] == "no-store"
    assert "--teal" in css.text
    assert javascript.status_code == 200
    assert "WATCHLIST_REVISION_CONFLICT" in javascript.text
    assert "预检通过，可以启动" in javascript.text
    assert "预检未通过" in javascript.text
    assert "X-TG-Hub-CSRF" in javascript.text


async def test_remote_client_cannot_load_admin_pages_or_assets(tmp_path: Path) -> None:
    async with _client(tmp_path, remote=True) as (client, _):
        page = await client.get("/admin/")
        asset = await client.get("/admin/static/admin.css")

    assert page.status_code == 403
    assert page.json()["error"]["code"] == "ADMIN_ORIGIN_REJECTED"
    assert asset.status_code == 403


async def test_page_has_landmarks_and_accessible_dialog_controls(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _):
        overview = await client.get("/admin/")
        watchlist = await client.get("/admin/watchlist")

    assert '<aside class="sidebar" aria-label="主导航">' in overview.text
    assert '<main class="main-area">' in overview.text
    assert 'aria-live="polite"' in overview.text
    assert 'role="tablist"' in watchlist.text
    assert 'aria-label="关闭"' in watchlist.text
