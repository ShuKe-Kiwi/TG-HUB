import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from app.config import Settings
from app.deploy.rotation_status import RotationStatusProjection
from app.main import create_app
from app.modules.monitor.control import (
    MonitorAllowedActions,
    MonitorControlError,
    MonitorControlSnapshot,
    MonitorStartResult,
    MonitorStopResult,
)
from app.modules.monitor.heartbeat import HeartbeatPersistenceStatus
from app.modules.monitor.preflight import MonitorStartupPreflightReport
from app.modules.monitor.watchlist_service import WatchlistApplicationService

_CSRF = "test-csrf-token"
_ORIGIN = "http://127.0.0.1"


def _write_watchlist(path: Path, title: str = "家业") -> None:
    path.write_text(
        json.dumps(
            {
                "source_channels": [
                    {"ref": "-1001234567890", "enabled": True}
                ],
                "watch_titles": [
                    {"title": title, "enabled": True, "aliases": []}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _preflight() -> MonitorStartupPreflightReport:
    return MonitorStartupPreflightReport(
        status="pass",
        watchlist_loaded="yes",
        watchlist_schema="pass",
        telethon_dependency="pass",
        telegram_api_id_configured="pass",
        telegram_api_hash_configured="pass",
        session_configured="pass",
        session_parent_exists="pass",
        session_parent_writable="pass",
        database_url_configured="pass",
        enabled_source_channels=1,
        invalid_source_channels=0,
        enabled_watch_titles=1,
        blockers=[],
    )


def _control_snapshot(revision: str | None) -> MonitorControlSnapshot:
    return MonitorControlSnapshot(
        control_state="stopped",
        runtime_state=None,
        liveness="no",
        readiness="no",
        connected="no",
        handler_registered="no",
        started_at=None,
        uptime_seconds=0,
        runtime_started_revision=None,
        current_watchlist_revision=revision,
        watchlist_status="valid",
        restart_required=False,
        heartbeat=None,
        heartbeat_persistence=HeartbeatPersistenceStatus(
            enabled=True,
            status="idle",
        ),
        last_summary=None,
        last_errors=[],
        last_error_code=None,
        operation_in_progress=False,
        allowed_actions=MonitorAllowedActions(
            can_start=True,
            can_stop=False,
            can_preflight=True,
        ),
        task_owned=False,
    )


class FakeControlService:
    def __init__(self, revision: str | None) -> None:
        self.revision = revision
        self.start_error: str | None = None
        self.stop_result = MonitorStopResult(
            status="already_stopped",
            control_state="stopped",
            task_owned=False,
        )
        self.start_calls: list[str | None] = []
        self.preflight_calls = 0
        self.stop_calls = 0
        self.shutdown_calls = 0
        self.status_calls = 0
        self.fail_status = False

    async def status(self):
        self.status_calls += 1
        if self.fail_status:
            raise RuntimeError("sensitive internal exception")
        return _control_snapshot(self.revision)

    async def run_preflight(self):
        self.preflight_calls += 1
        return _preflight()

    async def start(self, expected_revision):
        self.start_calls.append(expected_revision)
        if self.start_error is not None:
            raise MonitorControlError(self.start_error)
        return MonitorStartResult(
            runtime_started_revision=self.revision or "revision"
        )

    async def stop(self):
        self.stop_calls += 1
        return self.stop_result

    async def shutdown(self):
        self.shutdown_calls += 1
        return MonitorStopResult(
            status="already_stopped",
            control_state="stopped",
            task_owned=False,
        )


@asynccontextmanager
async def _client(
    tmp_path: Path,
    *,
    rotation_status_reader=None,
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeControlService, WatchlistApplicationService]]:
    path = tmp_path / "watchlist.json"
    _write_watchlist(path)
    watchlist = WatchlistApplicationService(path=path)
    revision = (await watchlist.get_snapshot()).revision
    control = FakeControlService(revision)
    application = create_app(
        app_settings=Settings(
            APP_ENV="test",
            WATCHLIST_PATH=path,
            TELEGRAM_WEBHOOK_SECRET="",
        ),
        watchlist_service=watchlist,
        monitor_control_service=control,
        admin_csrf_token=_CSRF,
        rotation_status_reader=rotation_status_reader or (
            lambda: RotationStatusProjection(
                agent_status="not_configured",
                status="never_run",
                stale="not_applicable",
                rotated_files=0,
                cleaned_archives=0,
                archive_bytes=0,
                active_bytes=0,
                archive_budget_status="within_budget",
                active_oversize=False,
                legacy_content_possible=False,
            )
        ),
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=_ORIGIN,
        ) as client:
            yield client, control, watchlist


def _mutation_headers(**updates: str) -> dict[str, str]:
    headers = {
        "Origin": _ORIGIN,
        "X-TG-Hub-CSRF": _CSRF,
    }
    headers.update(updates)
    return headers


async def test_status_is_versioned_desensitized_and_not_cached(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, control, _):
        response = await client.get("/api/admin/v1/monitor/status")

    payload = response.json()
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"] == payload["request_id"]
    assert payload["api_version"] == "v1"
    assert payload["schema_version"] == 1
    assert payload["data"]["allowed_actions"]["can_start"] is True
    serialized = response.text
    assert _CSRF not in serialized
    assert "DATABASE_URL" not in serialized
    assert control.status_calls == 1
    assert control.start_calls == []
    assert control.stop_calls == 0


async def test_rotation_status_is_independent_read_only_projection(
    tmp_path: Path,
) -> None:
    projection = RotationStatusProjection(
        agent_status="configured",
        status="fail",
        error_code="ROTATION_COMPRESS_FAILED",
        stale=False,
        rotated_files=0,
        cleaned_archives=0,
        archive_bytes=42,
        active_bytes=9,
        archive_budget_status="within_budget",
        active_oversize=False,
        legacy_content_possible=True,
    )
    async with _client(
        tmp_path, rotation_status_reader=lambda: projection
    ) as (client, control, _):
        response = await client.get("/api/admin/v1/observability/rotation")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["data"] == projection.model_dump(mode="json")
    assert control.status_calls == 0


async def test_rotation_reader_failure_degrades_without_breaking_admin(
    tmp_path: Path,
) -> None:
    def fail_reader():
        raise RuntimeError("sensitive /private/path exception")

    async with _client(tmp_path, rotation_status_reader=fail_reader) as (
        client,
        _,
        _,
    ):
        rotation = await client.get("/api/admin/v1/observability/rotation")
        monitor = await client.get("/api/admin/v1/monitor/status")
        live = await client.get("/health/live")

    assert rotation.status_code == 200
    assert rotation.json()["data"]["status"] == "invalid"
    assert rotation.json()["data"]["error_code"] == "ROTATION_STATUS_READ_FAILED"
    assert "sensitive" not in rotation.text
    assert "/private/path" not in rotation.text
    assert monitor.status_code == 200
    assert live.status_code == 200


async def test_watchlist_get_has_schema_version_and_validation(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _, _):
        response = await client.get("/api/admin/v1/watchlist")

    data = response.json()["data"]
    assert response.status_code == 200
    assert data["watchlist_schema_version"] == 1
    assert data["validation"] == {"valid": True, "error_code": None}
    assert data["config"]["watch_titles"][0]["title"] == "家业"


async def test_valid_mutations_map_to_control_and_watchlist_services(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, control, watchlist):
        revision = (await watchlist.get_snapshot()).revision
        preflight = await client.post(
            "/api/admin/v1/monitor/preflight",
            headers=_mutation_headers(),
            json={},
        )
        started = await client.post(
            "/api/admin/v1/monitor/start",
            headers=_mutation_headers(),
            json={"expected_watchlist_revision": revision},
        )
        stopped = await client.post(
            "/api/admin/v1/monitor/stop",
            headers=_mutation_headers(),
            json={},
        )
        replaced = await client.put(
            "/api/admin/v1/watchlist",
            headers=_mutation_headers(),
            json={
                "expected_revision": revision,
                "recovery_confirmed": False,
                "config": {
                    "source_channels": [
                        {"ref": "-1001234567890", "enabled": True}
                    ],
                    "watch_titles": [
                        {"title": "百花杀", "enabled": True, "aliases": []}
                    ],
                },
            },
        )

    assert preflight.status_code == 200
    assert started.status_code == 202
    assert started.json()["data"]["control_state"] == "starting"
    assert stopped.status_code == 200
    assert replaced.status_code == 200
    assert control.start_calls == [revision]
    assert control.preflight_calls == 1
    assert control.stop_calls == 1
    assert (await watchlist.get_snapshot()).config.watch_titles[0].title == "百花杀"


async def test_missing_or_wrong_csrf_is_rejected_before_body_validation(
    tmp_path: Path,
) -> None:
    async with _client(tmp_path) as (client, control, _):
        missing = await client.post(
            "/api/admin/v1/monitor/start",
            headers={"Origin": _ORIGIN},
            content="not-json",
        )
        wrong = await client.post(
            "/api/admin/v1/monitor/start",
            headers=_mutation_headers(**{"X-TG-Hub-CSRF": "wrong"}),
            json={},
        )
        missing_with_json = await client.post(
            "/api/admin/v1/monitor/start",
            headers={"Origin": _ORIGIN},
            json={},
        )

    assert missing.status_code == 415
    assert missing.json()["error"]["code"] == "INVALID_REQUEST"
    assert wrong.status_code == 403
    assert wrong.json()["error"]["code"] == "ADMIN_CSRF_REJECTED"
    assert missing_with_json.status_code == 403
    assert missing_with_json.json()["error"]["code"] == "ADMIN_CSRF_REJECTED"
    assert control.start_calls == []


async def test_foreign_origin_and_malicious_host_are_rejected(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _, _):
        foreign = await client.post(
            "/api/admin/v1/monitor/stop",
            headers=_mutation_headers(Origin="https://evil.example"),
            json={},
        )
        malicious_host = await client.get(
            "/api/admin/v1/monitor/status",
            headers={"Host": "evil.example"},
        )

    assert foreign.status_code == 403
    assert foreign.json()["error"]["code"] == "ADMIN_ORIGIN_REJECTED"
    assert malicious_host.status_code == 403
    assert malicious_host.json()["error"]["code"] == "ADMIN_ORIGIN_REJECTED"


async def test_remote_client_is_rejected_even_with_local_host_header(
    tmp_path: Path,
) -> None:
    path = tmp_path / "watchlist.json"
    _write_watchlist(path)
    watchlist = WatchlistApplicationService(path=path)
    control = FakeControlService((await watchlist.get_snapshot()).revision)
    application = create_app(
        app_settings=Settings(WATCHLIST_PATH=path),
        watchlist_service=watchlist,
        monitor_control_service=control,
        admin_csrf_token=_CSRF,
    )
    async with application.router.lifespan_context(application):
        transport = httpx.ASGITransport(
            app=application,
            client=("192.0.2.10", 43123),
        )
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
        ) as client:
            response = await client.get("/api/admin/v1/monitor/status")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_ORIGIN_REJECTED"
    assert control.status_calls == 0


async def test_foreign_origin_is_rejected_on_read_endpoint(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _, _):
        response = await client.get(
            "/api/admin/v1/watchlist",
            headers={"Origin": "https://evil.example"},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ADMIN_ORIGIN_REJECTED"


async def test_invalid_request_and_path_override_use_stable_error(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _, _):
        response = await client.put(
            "/api/admin/v1/watchlist",
            headers=_mutation_headers(),
            json={
                "expected_revision": None,
                "config": {},
                "watchlist_path": "/tmp/other.json",
            },
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "/tmp/other.json" not in response.text


async def test_control_errors_have_stable_http_mapping(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, control, _):
        control.start_error = "MONITOR_ALREADY_ACTIVE"
        conflict = await client.post(
            "/api/admin/v1/monitor/start",
            headers=_mutation_headers(),
            json={},
        )
        control.start_error = "MONITOR_PRECHECK_FAILED"
        unprocessable = await client.post(
            "/api/admin/v1/monitor/start",
            headers=_mutation_headers(),
            json={},
        )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "MONITOR_ALREADY_ACTIVE"
    assert unprocessable.status_code == 422
    assert unprocessable.json()["error"]["code"] == "MONITOR_PRECHECK_FAILED"


async def test_stop_timeout_is_conflict_and_retains_result(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, control, _):
        control.stop_result = MonitorStopResult(
            status="timeout",
            control_state="degraded",
            task_owned=True,
            error_code="MONITOR_STOP_TIMEOUT",
        )
        response = await client.post(
            "/api/admin/v1/monitor/stop",
            headers=_mutation_headers(),
            json={},
        )

    assert response.status_code == 409
    assert response.json()["data"]["task_owned"] is True
    assert response.json()["data"]["error_code"] == "MONITOR_STOP_TIMEOUT"


async def test_lifespan_owns_control_service_shutdown(tmp_path: Path) -> None:
    async with _client(tmp_path) as (_, control, _):
        assert control.shutdown_calls == 0

    assert control.shutdown_calls == 1


async def test_admin_api_does_not_enable_cors(tmp_path: Path) -> None:
    async with _client(tmp_path) as (client, _, _):
        response = await client.get("/api/admin/v1/monitor/status")

    assert "access-control-allow-origin" not in response.headers


async def test_unknown_path_and_internal_failure_use_stable_envelope(
    tmp_path: Path,
) -> None:
    async with _client(tmp_path) as (client, control, _):
        unknown = await client.get("/api/admin/v1/unknown")
        control.fail_status = True
        failed = await client.get("/api/admin/v1/monitor/status")

    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "INVALID_REQUEST"
    assert failed.status_code == 500
    assert failed.json()["error"]["code"] == "ADMIN_INTERNAL_ERROR"
    assert "sensitive internal exception" not in failed.text
