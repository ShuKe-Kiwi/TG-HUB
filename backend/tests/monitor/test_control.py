import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.modules.monitor.bootstrap import (
    MonitorAssemblyReport,
    MonitorBootstrapResult,
)
from app.modules.monitor.control import (
    MonitorControlError,
    MonitorControlService,
)
from app.modules.monitor.preflight import MonitorStartupPreflightReport
from app.modules.monitor.runtime import failed_monitor_runtime_summary
from app.modules.monitor.watchlist_service import WatchlistApplicationService


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


def _preflight(status: str = "pass") -> MonitorStartupPreflightReport:
    return MonitorStartupPreflightReport(
        status=status,
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
        blockers=[] if status == "pass" else ["blocked"],
    )


def _bootstrap_result(success: bool = True) -> MonitorBootstrapResult:
    summary = failed_monitor_runtime_summary("CONNECT_FAILED")
    if success:
        summary = summary.model_copy(
            update={
                "startup_status": "pass",
                "final_state": "stopped",
                "shutdown_reason": "operator_stop",
                "blockers": [],
                "errors": [],
            }
        )
    return MonitorBootstrapResult(
        assembly=MonitorAssemblyReport(
            database_ready="yes",
            bot_notification_status="disabled_config_missing",
            heartbeat_status="enabled",
            blockers=[],
        ),
        runtime=summary,
    )


class FakeRuntime:
    def __init__(self) -> None:
        self.state = "created"
        self.connected = False
        self.handler_registered = False

    def health(self):
        return SimpleNamespace(
            liveness="yes" if self.state != "failed" else "no",
            readiness=(
                "yes" if self.state == "listening" else "no"
            ),
        )


class FakeBootstrap:
    def __init__(
        self,
        *,
        finish_on_stop: bool = True,
        result: MonitorBootstrapResult | None = None,
    ) -> None:
        self.runtime = FakeRuntime()
        self.finish_on_stop = finish_on_stop
        self.result = result or _bootstrap_result()
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.stop_calls = 0
        self.close_calls = 0

    async def run(self) -> MonitorBootstrapResult:
        self.runtime.state = "listening"
        self.runtime.connected = True
        self.runtime.handler_registered = True
        self.started.set()
        await self.finish.wait()
        self.runtime.state = self.result.runtime.final_state
        return self.result

    def stop(self) -> None:
        self.stop_calls += 1
        if self.finish_on_stop:
            self.finish.set()

    async def aclose(self) -> None:
        self.close_calls += 1


class BootstrapFactory:
    def __init__(
        self,
        *,
        finish_on_stop: bool = True,
        result: MonitorBootstrapResult | None = None,
    ) -> None:
        self.finish_on_stop = finish_on_stop
        self.result = result
        self.instances: list[FakeBootstrap] = []

    def __call__(self, sink) -> FakeBootstrap:
        instance = FakeBootstrap(
            finish_on_stop=self.finish_on_stop,
            result=self.result,
        )
        self.instances.append(instance)
        return instance


def _service(
    tmp_path: Path,
    *,
    factory: BootstrapFactory | None = None,
    preflight_status: str = "pass",
    stop_timeout_seconds: float = 0.02,
) -> tuple[MonitorControlService, WatchlistApplicationService, BootstrapFactory]:
    path = tmp_path / "watchlist.json"
    _write_watchlist(path)
    watchlist = WatchlistApplicationService(path=path)
    resolved_factory = factory or BootstrapFactory()
    service = MonitorControlService(
        Settings(WATCHLIST_PATH=path),
        watchlist_service=watchlist,
        bootstrap_factory=resolved_factory,
        preflight_runner=lambda settings: _preflight(preflight_status),
        stop_timeout_seconds=stop_timeout_seconds,
    )
    return service, watchlist, resolved_factory


async def test_initial_state_is_stopped_with_server_allowed_actions(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    status = await service.status()

    assert status.control_state == "stopped"
    assert status.task_owned is False
    assert status.allowed_actions.can_start is True
    assert status.allowed_actions.can_stop is False
    assert status.restart_required is False


async def test_start_is_accepted_without_waiting_for_runtime_end(tmp_path: Path) -> None:
    service, _, factory = _service(tmp_path)
    revision = (await service.status()).current_watchlist_revision

    result = await service.start(revision)
    bootstrap = factory.instances[0]
    await bootstrap.started.wait()
    status = await service.status()

    assert result.accepted is True
    assert result.control_state == "starting"
    assert status.control_state == "running"
    assert status.readiness == "yes"
    assert status.allowed_actions.can_start is False
    assert status.allowed_actions.can_stop is True
    await service.stop()


async def test_repeated_start_never_creates_second_task(tmp_path: Path) -> None:
    service, _, factory = _service(tmp_path)
    await service.start()
    await factory.instances[0].started.wait()

    with pytest.raises(MonitorControlError) as exc_info:
        await service.start()

    assert exc_info.value.error_code == "MONITOR_ALREADY_ACTIVE"
    assert len(factory.instances) == 1
    await service.stop()


async def test_preflight_failure_prevents_task_creation(tmp_path: Path) -> None:
    service, _, factory = _service(tmp_path, preflight_status="fail")

    with pytest.raises(MonitorControlError) as exc_info:
        await service.start()

    assert exc_info.value.error_code == "MONITOR_PRECHECK_FAILED"
    assert factory.instances == []
    assert (await service.status()).control_state == "stopped"


async def test_expected_revision_conflict_prevents_start(tmp_path: Path) -> None:
    service, _, factory = _service(tmp_path)

    with pytest.raises(MonitorControlError) as exc_info:
        await service.start("stale")

    assert exc_info.value.error_code == "MONITOR_STATE_CONFLICT"
    assert factory.instances == []


async def test_preflight_is_rejected_while_start_operation_is_active(
    tmp_path: Path,
) -> None:
    service, _, factory = _service(tmp_path)
    await service.start()
    await factory.instances[0].started.wait()

    with pytest.raises(MonitorControlError) as exc_info:
        await service.run_preflight()

    assert exc_info.value.error_code == "MONITOR_CONTROL_BUSY"
    await service.stop()


async def test_stop_is_graceful_and_repeated_stop_is_idempotent(tmp_path: Path) -> None:
    service, _, factory = _service(tmp_path)
    await service.start()
    bootstrap = factory.instances[0]
    await bootstrap.started.wait()

    first = await service.stop()
    second = await service.stop()

    assert first.status == "stopped"
    assert first.control_state == "stopped"
    assert second.status == "already_stopped"
    assert bootstrap.stop_calls == 1
    assert bootstrap.close_calls == 1


async def test_stop_timeout_retains_task_and_blocks_new_start(tmp_path: Path) -> None:
    factory = BootstrapFactory(finish_on_stop=False)
    service, _, _ = _service(tmp_path, factory=factory)
    await service.start()
    bootstrap = factory.instances[0]
    await bootstrap.started.wait()

    result = await service.stop()
    status = await service.status()

    assert result.status == "timeout"
    assert result.task_owned is True
    assert status.control_state == "degraded"
    assert status.task_owned is True
    assert status.allowed_actions.can_start is False
    assert status.allowed_actions.can_stop is True
    with pytest.raises(MonitorControlError) as exc_info:
        await service.start()
    assert exc_info.value.error_code == "MONITOR_TASK_STILL_RUNNING"

    bootstrap.finish.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert (await service.status()).control_state == "stopped"


async def test_second_stop_after_timeout_targets_same_task(tmp_path: Path) -> None:
    factory = BootstrapFactory(finish_on_stop=False)
    service, _, _ = _service(tmp_path, factory=factory)
    await service.start()
    bootstrap = factory.instances[0]
    await bootstrap.started.wait()
    assert (await service.stop()).status == "timeout"

    bootstrap.finish_on_stop = True
    second = await service.stop()

    assert second.status == "stopped"
    assert bootstrap.stop_calls == 2
    assert len(factory.instances) == 1


async def test_runtime_revision_drift_requires_restart_only_while_active(
    tmp_path: Path,
) -> None:
    service, watchlist, factory = _service(tmp_path)
    await service.start()
    await factory.instances[0].started.wait()
    before = await watchlist.get_snapshot()
    await watchlist.replace(
        {
            "source_channels": [
                {"ref": "-1001234567890", "enabled": True}
            ],
            "watch_titles": [
                {"title": "百花杀", "enabled": True, "aliases": []}
            ],
        },
        before.revision,
    )

    active = await service.status()
    await service.stop()
    stopped = await service.status()

    assert active.restart_required is True
    assert active.runtime_started_revision != active.current_watchlist_revision
    assert stopped.restart_required is False


async def test_unexpected_task_cancellation_becomes_failed(tmp_path: Path) -> None:
    service, _, factory = _service(tmp_path)
    await service.start()
    await factory.instances[0].started.wait()
    task = service._task
    assert task is not None

    task.cancel()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    status = await service.status()

    assert status.control_state == "failed"
    assert status.last_error_code == "MONITOR_TASK_CANCELLED"
    assert status.allowed_actions.can_start is True


async def test_runtime_failure_is_retained_in_control_status(tmp_path: Path) -> None:
    factory = BootstrapFactory(result=_bootstrap_result(success=False))
    service, _, _ = _service(tmp_path, factory=factory)
    await service.start()
    bootstrap = factory.instances[0]
    await bootstrap.started.wait()
    bootstrap.finish.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    status = await service.status()

    assert status.control_state == "failed"
    assert status.last_summary is not None
    assert status.last_summary.startup_status == "fail"
    assert status.last_error_code == "CONNECT_FAILED"
    assert status.allowed_actions.can_start is True


async def test_lifespan_shutdown_does_not_create_second_stop_flow(tmp_path: Path) -> None:
    service, _, factory = _service(tmp_path)
    await service.start()
    bootstrap = factory.instances[0]
    await bootstrap.started.wait()

    first, second = await asyncio.gather(
        service.shutdown(),
        service.shutdown(),
    )

    assert {first.status, second.status} <= {"stopped", "already_stopped"}
    assert bootstrap.stop_calls == 1

    with pytest.raises(MonitorControlError) as exc_info:
        await service.start()
    assert exc_info.value.error_code == "MONITOR_STATE_CONFLICT"


async def test_watchlist_missing_during_run_does_not_stop_task(tmp_path: Path) -> None:
    service, watchlist, factory = _service(tmp_path)
    await service.start()
    bootstrap = factory.instances[0]
    await bootstrap.started.wait()
    watchlist.path.unlink()

    status = await service.status()

    assert status.watchlist_status == "missing"
    assert status.restart_required is True
    assert status.control_state == "running"
    assert status.task_owned is True
    await service.stop()
