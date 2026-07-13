"""Single-task application control boundary for the Web monitor mode."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.config import Settings, settings
from app.modules.monitor.bootstrap import MonitorBootstrap, MonitorBootstrapResult
from app.modules.monitor.heartbeat import (
    HeartbeatPersistenceStatus,
    HeartbeatSink,
    JsonlHeartbeatSink,
    create_heartbeat_sinks,
)
from app.modules.monitor.preflight import (
    MonitorStartupPreflightReport,
    run_static_startup_preflight,
)
from app.modules.monitor.runtime import (
    MonitorHeartbeat,
    MonitorRuntimeError,
    MonitorRuntimeSummary,
    RuntimeState,
)
from app.modules.monitor.watchlist_service import (
    WatchlistApplicationService,
    WatchlistStatus,
)

ControlState = Literal[
    "stopped",
    "starting",
    "running",
    "stopping",
    "degraded",
    "failed",
]
ControlErrorCode = Literal[
    "MONITOR_PRECHECK_FAILED",
    "MONITOR_ALREADY_ACTIVE",
    "MONITOR_START_FAILED",
    "MONITOR_STOP_TIMEOUT",
    "MONITOR_CONTROL_BUSY",
    "MONITOR_TASK_STILL_RUNNING",
    "MONITOR_STATE_CONFLICT",
    "WATCHLIST_NOT_READY",
    "MONITOR_TASK_CANCELLED",
]


class MonitorControlError(RuntimeError):
    def __init__(self, error_code: ControlErrorCode) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class MonitorAllowedActions(BaseModel):
    model_config = ConfigDict(frozen=True)

    can_start: bool
    can_stop: bool
    can_preflight: bool


class MonitorControlSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    control_state: ControlState
    runtime_state: RuntimeState | None
    liveness: Literal["yes", "no"]
    readiness: Literal["yes", "no"]
    connected: Literal["yes", "no"]
    handler_registered: Literal["yes", "no"]
    started_at: datetime | None
    uptime_seconds: float
    runtime_started_revision: str | None
    current_watchlist_revision: str | None
    watchlist_status: WatchlistStatus
    restart_required: bool
    heartbeat: MonitorHeartbeat | None
    heartbeat_persistence: HeartbeatPersistenceStatus
    last_summary: MonitorRuntimeSummary | None
    last_errors: list[MonitorRuntimeError]
    last_error_code: str | None
    operation_in_progress: bool
    allowed_actions: MonitorAllowedActions
    task_owned: bool
    report_desensitized: Literal["yes"] = "yes"


class MonitorStartResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    accepted: Literal[True] = True
    control_state: Literal["starting"] = "starting"
    runtime_started_revision: str
    report_desensitized: Literal["yes"] = "yes"


class MonitorStopResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["stopped", "already_stopped", "timeout"]
    control_state: ControlState
    task_owned: bool
    error_code: ControlErrorCode | None = None
    report_desensitized: Literal["yes"] = "yes"


class BootstrapLike(Protocol):
    runtime: Any | None

    async def run(self) -> MonitorBootstrapResult: ...

    def stop(self) -> None: ...

    async def aclose(self) -> None: ...


BootstrapFactory = Callable[[HeartbeatSink], BootstrapLike]
PreflightRunner = Callable[[Settings], MonitorStartupPreflightReport]


class ControlHeartbeatSink:
    """Keep only the latest desensitized heartbeat for status polling."""

    def __init__(self, callback: Callable[[MonitorHeartbeat], Any]) -> None:
        self.latest: MonitorHeartbeat | None = None
        self._callback = callback
        self.error_count = 0
        self.last_error_code: str | None = None

    async def emit(self, heartbeat: MonitorHeartbeat) -> None:
        self.latest = heartbeat
        try:
            result = self._callback(heartbeat)
            if inspect.isawaitable(result):
                await result
        except Exception:
            self.error_count += 1
            self.last_error_code = "CONTROL_HEARTBEAT_FAILED"

    async def aclose(self) -> None:
        return None


class MonitorControlService:
    """Own exactly one Web-controlled monitor task at a time."""

    def __init__(
        self,
        app_settings: Settings | None = None,
        *,
        watchlist_service: WatchlistApplicationService | None = None,
        bootstrap_factory: BootstrapFactory | None = None,
        preflight_runner: PreflightRunner = run_static_startup_preflight,
        stop_timeout_seconds: float = 10.0,
    ) -> None:
        self.settings = app_settings or settings
        self.watchlist_service = watchlist_service or WatchlistApplicationService(
            self.settings
        )
        self._bootstrap_factory = bootstrap_factory or (
            lambda sink: MonitorBootstrap(
                self.settings,
                heartbeat_sink=sink,
            )
        )
        self._preflight_runner = preflight_runner
        self._stop_timeout_seconds = stop_timeout_seconds
        self._lock = asyncio.Lock()
        self._state: ControlState = "stopped"
        self._task: asyncio.Task[None] | None = None
        self._bootstrap: BootstrapLike | None = None
        self._heartbeat_sink: ControlHeartbeatSink | None = None
        self._persistence_sink: JsonlHeartbeatSink | None = None
        self._runtime_started_revision: str | None = None
        self._started_at: datetime | None = None
        self._last_summary: MonitorRuntimeSummary | None = None
        self._last_error_code: str | None = None
        self._stop_timed_out = False
        self._shutdown_task: asyncio.Task[MonitorStopResult] | None = None

    async def status(self) -> MonitorControlSnapshot:
        watchlist = await self.watchlist_service.get_snapshot()
        async with self._lock:
            self._refresh_from_runtime_locked()
            heartbeat = (
                self._heartbeat_sink.latest
                if self._heartbeat_sink is not None
                else None
            )
            persistence_status = (
                self._persistence_sink.status()
                if self._persistence_sink is not None
                else HeartbeatPersistenceStatus(enabled=True, status="idle")
            )
            runtime = getattr(self._bootstrap, "runtime", None)
            runtime_state = getattr(runtime, "state", None)
            if runtime_state is None and heartbeat is not None:
                runtime_state = heartbeat.monitor_state
            task_owned = self._task is not None
            task_active = task_owned and not self._task.done()
            operation_in_progress = self._state in {"starting", "stopping"}
            allowed = _allowed_actions(
                self._state,
                task_active=task_active,
                operation_in_progress=operation_in_progress,
            )
            active = task_active and self._state in {
                "starting",
                "running",
                "stopping",
                "degraded",
            }
            restart_required = (
                active
                and self._runtime_started_revision
                != watchlist.revision
            )
            liveness, readiness, connected, handler_registered, uptime = (
                _runtime_observation(runtime, heartbeat, self._started_at)
            )
            last_errors = (
                list(self._last_summary.errors)
                if self._last_summary is not None
                else []
            )
            return MonitorControlSnapshot(
                control_state=self._state,
                runtime_state=runtime_state,
                liveness=liveness,
                readiness=readiness,
                connected=connected,
                handler_registered=handler_registered,
                started_at=self._started_at,
                uptime_seconds=uptime,
                runtime_started_revision=self._runtime_started_revision,
                current_watchlist_revision=watchlist.revision,
                watchlist_status=watchlist.status,
                restart_required=restart_required,
                heartbeat=heartbeat,
                heartbeat_persistence=persistence_status,
                last_summary=self._last_summary,
                last_errors=last_errors,
                last_error_code=self._last_error_code,
                operation_in_progress=operation_in_progress,
                allowed_actions=allowed,
                task_owned=task_owned,
            )

    async def run_preflight(self) -> MonitorStartupPreflightReport:
        async with self._lock:
            if self._state in {"starting", "stopping"}:
                raise MonitorControlError("MONITOR_CONTROL_BUSY")
        return await asyncio.to_thread(self._preflight_runner, self.settings)

    async def start(
        self,
        expected_watchlist_revision: str | None = None,
    ) -> MonitorStartResult:
        async with self._lock:
            if self._shutdown_task is not None:
                raise MonitorControlError("MONITOR_STATE_CONFLICT")
            if self._task is not None and not self._task.done():
                if self._state == "degraded":
                    raise MonitorControlError(
                        "MONITOR_TASK_STILL_RUNNING"
                    )
                raise MonitorControlError("MONITOR_ALREADY_ACTIVE")
            if self._state in {"starting", "running", "stopping"}:
                raise MonitorControlError("MONITOR_ALREADY_ACTIVE")

            snapshot = await self.watchlist_service.get_snapshot()
            if snapshot.status != "valid" or snapshot.revision is None:
                raise MonitorControlError("WATCHLIST_NOT_READY")
            if (
                expected_watchlist_revision is not None
                and expected_watchlist_revision != snapshot.revision
            ):
                raise MonitorControlError("MONITOR_STATE_CONFLICT")

            preflight = await asyncio.to_thread(
                self._preflight_runner,
                self.settings,
            )
            if preflight.status != "pass":
                self._last_error_code = "MONITOR_PRECHECK_FAILED"
                raise MonitorControlError("MONITOR_PRECHECK_FAILED")

            control_sink = ControlHeartbeatSink(self._on_heartbeat)
            sink, persistence_sink = create_heartbeat_sinks(
                self.settings.HEARTBEAT_PATH,
                control_sink=control_sink,
            )
            try:
                bootstrap = self._bootstrap_factory(sink)
            except Exception as exc:
                await sink.aclose()
                self._last_error_code = "MONITOR_START_FAILED"
                raise MonitorControlError("MONITOR_START_FAILED") from exc

            self._heartbeat_sink = control_sink
            self._persistence_sink = persistence_sink
            self._bootstrap = bootstrap
            self._runtime_started_revision = snapshot.revision
            self._started_at = datetime.now(timezone.utc)
            self._last_summary = None
            self._last_error_code = None
            self._stop_timed_out = False
            self._state = "starting"
            task = asyncio.create_task(
                self._run_owned_bootstrap(bootstrap),
                name="tg-hub-monitor-control",
            )
            self._task = task
            return MonitorStartResult(
                runtime_started_revision=snapshot.revision
            )

    async def stop(self) -> MonitorStopResult:
        async with self._lock:
            task = self._task
            bootstrap = self._bootstrap
            if task is None or task.done():
                return MonitorStopResult(
                    status="already_stopped",
                    control_state=self._state,
                    task_owned=False,
                )
            self._state = "stopping"
            if bootstrap is not None:
                bootstrap.stop()

        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._stop_timeout_seconds,
            )
        except asyncio.TimeoutError:
            async with self._lock:
                self._state = "degraded"
                self._last_error_code = "MONITOR_STOP_TIMEOUT"
                self._stop_timed_out = True
                return MonitorStopResult(
                    status="timeout",
                    control_state="degraded",
                    task_owned=self._task is task,
                    error_code="MONITOR_STOP_TIMEOUT",
                )

        async with self._lock:
            return MonitorStopResult(
                status="stopped",
                control_state=self._state,
                task_owned=self._task is not None,
            )

    async def shutdown(self) -> MonitorStopResult:
        async with self._lock:
            shutdown_task = self._shutdown_task
            if shutdown_task is None:
                shutdown_task = asyncio.create_task(
                    self.stop(),
                    name="tg-hub-monitor-shutdown",
                )
                self._shutdown_task = shutdown_task
        return await asyncio.shield(shutdown_task)

    async def _run_owned_bootstrap(
        self,
        bootstrap: BootstrapLike,
    ) -> None:
        result: MonitorBootstrapResult | None = None
        cancelled = False
        try:
            result = await bootstrap.run()
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            pass
        finally:
            try:
                await bootstrap.aclose()
            except Exception:
                pass

        current_task = asyncio.current_task()
        async with self._lock:
            if result is not None:
                self._last_summary = result.runtime
                if (
                    result.runtime.startup_status == "fail"
                    or result.runtime.final_state == "failed"
                ):
                    self._state = "failed"
                    self._last_error_code = _summary_error_code(result)
                else:
                    self._state = "stopped"
                    self._last_error_code = None
            elif cancelled:
                self._state = "failed"
                self._last_error_code = "MONITOR_TASK_CANCELLED"
            else:
                self._state = "failed"
                self._last_error_code = "MONITOR_START_FAILED"

            if self._task is current_task:
                self._task = None
                self._bootstrap = None
                self._stop_timed_out = False

    async def _on_heartbeat(self, heartbeat: MonitorHeartbeat) -> None:
        async with self._lock:
            if self._state == "stopping":
                return
            if heartbeat.monitor_state == "listening":
                self._state = "running"
            elif heartbeat.monitor_state == "degraded":
                self._state = "degraded"
            elif heartbeat.monitor_state == "failed":
                self._state = "failed"

    def _refresh_from_runtime_locked(self) -> None:
        if self._state == "stopping" or self._stop_timed_out:
            return
        runtime = getattr(self._bootstrap, "runtime", None)
        runtime_state = getattr(runtime, "state", None)
        if runtime_state == "listening":
            self._state = "running"
        elif runtime_state == "degraded":
            self._state = "degraded"
        elif runtime_state == "failed":
            self._state = "failed"


def _allowed_actions(
    state: ControlState,
    *,
    task_active: bool,
    operation_in_progress: bool,
) -> MonitorAllowedActions:
    return MonitorAllowedActions(
        can_start=(
            not task_active
            and not operation_in_progress
            and state in {"stopped", "failed"}
        ),
        can_stop=task_active and state in {
            "starting",
            "running",
            "stopping",
            "degraded",
        },
        can_preflight=not operation_in_progress,
    )


def _runtime_observation(
    runtime: Any | None,
    heartbeat: MonitorHeartbeat | None,
    started_at: datetime | None,
) -> tuple[
    Literal["yes", "no"],
    Literal["yes", "no"],
    Literal["yes", "no"],
    Literal["yes", "no"],
    float,
]:
    if heartbeat is not None:
        return (
            heartbeat.liveness,
            heartbeat.readiness,
            heartbeat.connected,
            heartbeat.handler_registered,
            heartbeat.uptime_seconds,
        )
    if runtime is not None:
        health = runtime.health() if hasattr(runtime, "health") else None
        liveness = getattr(health, "liveness", "no")
        readiness = getattr(health, "readiness", "no")
        connected = "yes" if getattr(runtime, "connected", False) else "no"
        registered = (
            "yes" if getattr(runtime, "handler_registered", False) else "no"
        )
        uptime = 0.0
        if started_at is not None:
            uptime = max(
                (datetime.now(timezone.utc) - started_at).total_seconds(),
                0.0,
            )
        return liveness, readiness, connected, registered, uptime
    return "no", "no", "no", "no", 0.0


def _summary_error_code(result: MonitorBootstrapResult) -> str:
    if result.runtime.errors:
        return result.runtime.errors[-1].error_code
    if result.assembly.blockers:
        return result.assembly.blockers[-1]
    return "MONITOR_START_FAILED"
