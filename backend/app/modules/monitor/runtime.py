"""Long-running monitor runtime lifecycle.

P6-2D owns monitor process lifecycle only. It receives Telegram events,
converts them to ``IncomingMessage``, runs the watchlist filter, and updates
desensitized runtime counters. It must not access the database, parser,
normalizer, dedup, bot notification, media download, history backfill, or raw
event persistence paths.
"""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, settings
from app.modules.monitor.config import WatchlistConfig, load_watchlist
from app.modules.monitor.filter import filter_message
from app.modules.monitor.listener_dry_run import (
    IncomingMessageAdapter,
    TelethonIncomingMessageAdapter,
    TelegramClientLike,
    create_telethon_client_from_settings,
)
from app.modules.monitor.resolver import (
    ChannelResolver,
    ResolvedSourceChannelReport,
    resolve_source_channels,
)

ReportFlag = Literal["yes", "no"]
Recoverability = Literal["transient", "fatal"]
RuntimeState = Literal[
    "created",
    "starting",
    "preflight",
    "resolving_channels",
    "connecting",
    "registering_handler",
    "listening",
    "degraded",
    "draining",
    "stopped",
    "failed",
]
RuntimeErrorCode = Literal[
    "WATCHLIST_UNREADABLE",
    "WATCHLIST_SCHEMA_INVALID",
    "NO_ENABLED_CHANNELS",
    "CHANNEL_RESOLUTION_FAILED",
    "TELETHON_DEPENDENCY_MISSING",
    "TELEGRAM_CREDENTIALS_MISSING",
    "SESSION_UNAVAILABLE",
    "CONNECT_FAILED",
    "HANDLER_REGISTRATION_FAILED",
    "CLIENT_DISCONNECTED",
    "RECONNECT_EXHAUSTED",
    "DTO_CONVERSION_ERROR",
    "HANDLER_ERROR",
    "FILTER_ERROR",
    "INGESTION_ERROR",
    "INGESTION_INVALID_RESULT",
    "PROCESSING_BOUNDARY_EXCEPTION",
    "PROCESSING_INVALID_RESULT",
    "FLOOD_WAIT",
    "ACCESS_FORBIDDEN",
    "CHANNEL_PRIVATE",
    "UNEXPECTED_EXCEPTION",
    "HANDLER_REMOVE_FAILED",
    "DISCONNECT_FAILED",
    "DRAIN_TIMEOUT",
]
RuntimeErrorPhase = Literal[
    "startup",
    "connect",
    "handler",
    "runtime",
    "reconnect",
    "shutdown",
]
ShutdownReason = Literal[
    "not_started",
    "operator_stop",
    "client_disconnected",
    "startup_failed",
    "runtime_failed",
    "cancelled",
]
SleepFunc = Callable[[float], Awaitable[None]]
HeartbeatSink = Callable[["MonitorHeartbeat"], Any]
EventBuilderFactory = Callable[[tuple[int, ...]], Any]
IngestionStatus = Literal[
    "stored",
    "duplicate",
    "channel_not_registered",
    "invalid_source_ref",
    "invalid_message_id",
    "empty_content",
    "ingest_failed",
]
ProcessingStatus = Literal[
    "invalid_raw_message_id",
    "raw_message_not_found",
    "already_processed",
    "parse_failed",
    "dedup_skipped",
    "dedup_new",
    "dedup_matched",
    "dedup_failed",
    "processing_failed",
]
_INGESTION_STATUSES = {
    "stored",
    "duplicate",
    "channel_not_registered",
    "invalid_source_ref",
    "invalid_message_id",
    "empty_content",
    "ingest_failed",
}
_PROCESSING_STATUSES = {
    "invalid_raw_message_id",
    "raw_message_not_found",
    "already_processed",
    "parse_failed",
    "dedup_skipped",
    "dedup_new",
    "dedup_matched",
    "dedup_failed",
    "processing_failed",
}
_PROCESSING_SUCCESS_STATUSES = {"dedup_new", "dedup_matched", "dedup_skipped"}


class MonitorRuntimeConfig(BaseModel):
    """Runtime lifecycle knobs for P6-2D."""

    model_config = ConfigDict(frozen=True)

    heartbeat_interval_seconds: float = Field(default=30, gt=0)
    reconnect_initial_delay_seconds: float = Field(default=1, ge=0)
    reconnect_max_delay_seconds: float = Field(default=60, ge=0)
    reconnect_stable_reset_seconds: float = Field(default=300, ge=0)
    drain_timeout_seconds: float = Field(default=10, ge=0)
    max_inflight_events: int = Field(default=100, gt=0)
    reconnect_jitter: bool = True
    max_reconnect_attempts: int | None = Field(default=None, ge=0)

    @classmethod
    def from_settings(cls, app_settings: Settings | None = None) -> "MonitorRuntimeConfig":
        resolved_settings = app_settings or settings
        return cls(
            heartbeat_interval_seconds=resolved_settings.MONITOR_HEARTBEAT_INTERVAL_SECONDS,
            reconnect_initial_delay_seconds=resolved_settings.MONITOR_RECONNECT_INITIAL_DELAY_SECONDS,
            reconnect_max_delay_seconds=resolved_settings.MONITOR_RECONNECT_MAX_DELAY_SECONDS,
            reconnect_stable_reset_seconds=resolved_settings.MONITOR_RECONNECT_STABLE_RESET_SECONDS,
            drain_timeout_seconds=resolved_settings.MONITOR_DRAIN_TIMEOUT_SECONDS,
            max_inflight_events=resolved_settings.MONITOR_MAX_INFLIGHT_EVENTS,
        )


class MonitorRuntimeError(BaseModel):
    """Stable, desensitized runtime error report."""

    model_config = ConfigDict(frozen=True)

    error_code: RuntimeErrorCode
    error_phase: RuntimeErrorPhase
    recoverability: Recoverability
    retry_scheduled: ReportFlag
    backoff_seconds: float
    occurred_at: datetime
    message_desensitized: Literal["yes"] = "yes"


class MonitorHealth(BaseModel):
    """Liveness/readiness view derived from runtime state."""

    model_config = ConfigDict(frozen=True)

    monitor_state: RuntimeState
    liveness: ReportFlag
    readiness: ReportFlag


class MonitorHeartbeat(BaseModel):
    """Desensitized heartbeat payload."""

    model_config = ConfigDict(frozen=True)

    monitor_state: RuntimeState
    uptime_seconds: float
    connected: ReportFlag
    handler_registered: ReportFlag
    enabled_source_channels: int
    resolved_channel_count: int
    events_seen_total: int
    events_matched_total: int
    events_rejected_total: int
    ingest_attempt_total: int
    ingest_stored_total: int
    ingest_duplicate_total: int
    ingest_rejected_total: int
    ingest_failed_total: int
    last_ingest_at: datetime | None
    last_ingest_error_code: str | None
    processing_enabled: ReportFlag
    process_attempt_total: int
    process_success_total: int
    process_already_done_total: int
    process_failed_total: int
    parse_executed_total: int
    dedup_executed_total: int
    last_process_at: datetime | None
    last_process_status: str | None
    last_process_error_code: str | None
    conversion_error_total: int
    handler_error_total: int
    reconnect_attempt_total: int
    last_event_at: datetime | None
    last_successful_event_at: datetime | None
    last_error_at: datetime | None
    last_error_code: RuntimeErrorCode | None
    backoff_seconds_current: float
    traffic_observed: ReportFlag
    liveness: ReportFlag
    readiness: ReportFlag
    heartbeat_total: int
    history_backfill_called: Literal["no"] = "no"
    raw_event_persisted: Literal["no"] = "no"
    production_ingest_enabled: ReportFlag
    report_desensitized: Literal["yes"] = "yes"


class MonitorRuntimeSummary(BaseModel):
    """Final P6-2D runtime summary."""

    model_config = ConfigDict(frozen=True)

    startup_status: Literal["pass", "fail"]
    final_state: RuntimeState
    uptime_seconds: float
    enabled_source_channels: int
    resolved_channel_count: int
    handler_registered_final: ReportFlag
    connected_final: ReportFlag
    events_seen_total: int
    events_matched_total: int
    events_rejected_total: int
    ingest_attempt_total: int
    ingest_stored_total: int
    ingest_duplicate_total: int
    ingest_rejected_total: int
    ingest_failed_total: int
    last_ingest_at: datetime | None
    last_ingest_error_code: str | None
    processing_enabled: ReportFlag
    process_attempt_total: int
    process_success_total: int
    process_already_done_total: int
    process_failed_total: int
    parse_executed_total: int
    dedup_executed_total: int
    last_process_at: datetime | None
    last_process_status: str | None
    last_process_error_code: str | None
    dto_conversion_error_total: int
    handler_error_total: int
    filter_error_total: int
    reconnect_attempt_total: int
    reconnect_success_total: int
    shutdown_total: int
    shutdown_reason: ShutdownReason
    handler_removed: ReportFlag
    client_disconnected_cleanly: ReportFlag
    heartbeat_emitted: ReportFlag
    report_desensitized: Literal["yes"] = "yes"
    media_downloaded: Literal["no"] = "no"
    history_backfill_called: Literal["no"] = "no"
    raw_event_persisted: Literal["no"] = "no"
    production_ingest_enabled: ReportFlag
    blockers: list[str]
    errors: list[MonitorRuntimeError]


class MonitorRuntimeClientFactory(Protocol):
    def __call__(self) -> TelegramClientLike:
        """Create a Telethon-compatible client without starting handlers."""


class IngestionBoundaryResult(Protocol):
    status: IngestionStatus
    error_code: str | None
    raw_message_id: int | None


class IncomingMessageIngestionBoundary(Protocol):
    async def ingest_incoming(self, message: Any) -> IngestionBoundaryResult:
        """Ingest one matched IncomingMessage through an application boundary."""


class ProcessingBoundaryResult(Protocol):
    status: ProcessingStatus
    raw_message_id: int | None
    error_code: str | None
    parse_executed: bool
    dedup_executed: bool
    eventbus_enabled: bool


class RawMessageProcessingBoundary(Protocol):
    async def process_raw_message(self, raw_message_id: int) -> ProcessingBoundaryResult:
        """Process one persisted RawMessage through an application boundary."""


class MonitorRuntime:
    """Own one long-running monitor lifecycle."""

    def __init__(
        self,
        *,
        watchlist: WatchlistConfig,
        client: TelegramClientLike,
        resolved_channel_ids: tuple[int, ...],
        config: MonitorRuntimeConfig | None = None,
        adapter: IncomingMessageAdapter | None = None,
        event_builder_factory: EventBuilderFactory | None = None,
        heartbeat_sink: HeartbeatSink | None = None,
        sleep: SleepFunc | None = None,
        ingestion_boundary: IncomingMessageIngestionBoundary | None = None,
        processing_boundary: RawMessageProcessingBoundary | None = None,
    ) -> None:
        self.watchlist = watchlist
        self.client = client
        self.resolved_channel_ids = tuple(resolved_channel_ids)
        self.config = config or MonitorRuntimeConfig()
        self.adapter = adapter or TelethonIncomingMessageAdapter()
        self.event_builder_factory = event_builder_factory
        self.heartbeat_sink = heartbeat_sink
        self.sleep = sleep or asyncio.sleep
        self.ingestion_boundary = ingestion_boundary
        self.processing_boundary = processing_boundary

        self.state: RuntimeState = "created"
        self.started_at: datetime | None = None
        self.stopped_at: datetime | None = None
        self.last_event_at: datetime | None = None
        self.last_successful_event_at: datetime | None = None
        self.last_matched_event_at: datetime | None = None
        self.last_error_at: datetime | None = None
        self.last_reconnect_at: datetime | None = None
        self.last_heartbeat_at: datetime | None = None

        self.events_seen_total = 0
        self.events_matched_total = 0
        self.events_rejected_total = 0
        self.ingest_attempt_total = 0
        self.ingest_stored_total = 0
        self.ingest_duplicate_total = 0
        self.ingest_rejected_total = 0
        self.ingest_failed_total = 0
        self.process_attempt_total = 0
        self.process_success_total = 0
        self.process_already_done_total = 0
        self.process_failed_total = 0
        self.parse_executed_total = 0
        self.dedup_executed_total = 0
        self.dto_conversion_error_total = 0
        self.handler_error_total = 0
        self.filter_error_total = 0
        self.reconnect_attempt_total = 0
        self.reconnect_success_total = 0
        self.heartbeat_total = 0
        self.shutdown_total = 0
        self.inflight_handler_tasks = 0

        self.connected = False
        self.handler_registered = False
        self.handler_removed = False
        self.client_disconnected_cleanly = False
        self.backoff_seconds_current = 0.0
        self.shutdown_reason: ShutdownReason = "not_started"
        self.last_ingest_at: datetime | None = None
        self.last_ingest_error_code: str | None = None
        self.last_process_at: datetime | None = None
        self.last_process_status: str | None = None
        self.last_process_error_code: str | None = None

        self._stop_requested = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._handler_callback: Callable[[object], Awaitable[None]] | None = None
        self._event_builder: Any | None = None
        self._summary: MonitorRuntimeSummary | None = None
        self._final_summary_emitted = False
        self._blockers: list[str] = []
        self._errors: list[MonitorRuntimeError] = []

    def health(self) -> MonitorHealth:
        readiness = (
            self.state == "listening"
            and self.connected
            and self.handler_registered
        )
        liveness = self.state not in {"stopped", "failed"}
        return MonitorHealth(
            monitor_state=self.state,
            liveness=_flag(liveness),
            readiness=_flag(readiness),
        )

    def stop(self) -> None:
        """Request graceful shutdown."""
        self._stop_requested.set()

    async def run(self) -> MonitorRuntimeSummary:
        """Run until stopped, failed, cancelled, or reconnect is exhausted."""
        if self._summary is not None:
            return self._summary

        self.started_at = _utcnow()
        try:
            if not self._startup_validate():
                self.shutdown_reason = "startup_failed"
                self.state = "failed"
            else:
                try:
                    await self._connect_and_register()
                except Exception:
                    if self.state == "failed":
                        self.shutdown_reason = "startup_failed"
                    else:
                        raise

            if self.state == "listening":
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            while not self._stop_requested.is_set() and self.state == "listening":
                exit_reason = await self._wait_until_stop_or_disconnect()
                if exit_reason == "stop":
                    self.shutdown_reason = "operator_stop"
                    break

                self.shutdown_reason = "client_disconnected"
                if not await self._reconnect_after_disconnect():
                    if self.shutdown_reason == "client_disconnected":
                        self.shutdown_reason = "runtime_failed"
                    break

        except asyncio.CancelledError:
            self.shutdown_reason = "cancelled"
            raise
        except Exception:
            self._record_error(
                "UNEXPECTED_EXCEPTION",
                phase="runtime",
                recoverability="fatal",
            )
            self.shutdown_reason = "runtime_failed"
            self.state = "failed"
        finally:
            await self._shutdown()

        return self._build_summary()

    def _startup_validate(self) -> bool:
        self.state = "starting"
        self.state = "preflight"

        enabled_source_channels = len(self.watchlist.enabled_source_refs())
        if enabled_source_channels == 0:
            self._blockers.append("no_enabled_source_channels")
            self._record_error(
                "NO_ENABLED_CHANNELS",
                phase="startup",
                recoverability="fatal",
            )
            return False

        self.state = "resolving_channels"
        if not self.resolved_channel_ids:
            self._blockers.append("empty_resolved_channel_ids")
            self._record_error(
                "CHANNEL_RESOLUTION_FAILED",
                phase="startup",
                recoverability="fatal",
            )
            return False

        if len(self.resolved_channel_ids) < enabled_source_channels:
            self._blockers.append("channel_resolution_incomplete")
            self._record_error(
                "CHANNEL_RESOLUTION_FAILED",
                phase="startup",
                recoverability="fatal",
            )
            return False

        return True

    async def _connect_and_register(self) -> None:
        self.state = "connecting"
        try:
            await _maybe_await(self.client.connect())
            self.connected = True
        except Exception as exc:
            self.connected = False
            self.state = "failed"
            self._blockers.append("connect_failed")
            self._record_error(
                _connect_error_code(exc),
                phase="connect",
                recoverability="transient",
            )
            raise

        self.state = "registering_handler"
        self._event_builder = _build_new_message_event(
            self.resolved_channel_ids,
            self.event_builder_factory,
        )
        self._handler_callback = self._handle_event
        try:
            await _maybe_await(
                self.client.add_event_handler(
                    self._handler_callback,
                    self._event_builder,
                )
            )
            self.handler_registered = True
            self.handler_removed = False
            self.state = "listening"
            self.backoff_seconds_current = 0.0
        except Exception:
            self._blockers.append("handler_registration_failed")
            self._record_error(
                "HANDLER_REGISTRATION_FAILED",
                phase="handler",
                recoverability="fatal",
            )
            self.state = "failed"
            try:
                await _maybe_await(self.client.disconnect())
                self.client_disconnected_cleanly = True
                self.connected = False
            except Exception:
                self._record_error(
                    "DISCONNECT_FAILED",
                    phase="shutdown",
                    recoverability="fatal",
                )
            raise

    async def _handle_event(self, event: object) -> None:
        if self.state != "listening" or self._stop_requested.is_set():
            return
        if self.inflight_handler_tasks >= self.config.max_inflight_events:
            self.events_rejected_total += 1
            self.handler_error_total += 1
            self._record_error(
                "HANDLER_ERROR",
                phase="handler",
                recoverability="transient",
            )
            return

        self.inflight_handler_tasks += 1
        try:
            incoming = self.adapter.from_telethon_event(event)
            self.last_event_at = _utcnow()
            result = filter_message(incoming, self.watchlist)
            self.events_seen_total += 1
            self.last_successful_event_at = self.last_event_at
            if result.matched:
                self.events_matched_total += 1
                self.last_matched_event_at = self.last_event_at
                await self._ingest_matched(incoming)
            else:
                self.events_rejected_total += 1
        except ValueError:
            self.dto_conversion_error_total += 1
            self.handler_error_total += 1
            self._record_error(
                "DTO_CONVERSION_ERROR",
                phase="handler",
                recoverability="transient",
            )
        except Exception:
            self.handler_error_total += 1
            self.filter_error_total += 1
            self._record_error(
                "FILTER_ERROR",
                phase="handler",
                recoverability="transient",
            )
        finally:
            self.inflight_handler_tasks -= 1

    async def _ingest_matched(self, incoming: Any) -> None:
        if self.ingestion_boundary is None:
            return

        self.ingest_attempt_total += 1
        self.last_ingest_at = _utcnow()
        try:
            result = await self.ingestion_boundary.ingest_incoming(incoming)
        except Exception:
            self.ingest_failed_total += 1
            self.last_ingest_error_code = "INGESTION_BOUNDARY_EXCEPTION"
            self._record_error(
                "INGESTION_ERROR",
                phase="handler",
                recoverability="transient",
            )
            return

        status = getattr(result, "status", None)
        error_code = getattr(result, "error_code", None)
        if status not in _INGESTION_STATUSES:
            self.ingest_failed_total += 1
            self.last_ingest_error_code = "INGESTION_INVALID_RESULT"
            self._record_error(
                "INGESTION_INVALID_RESULT",
                phase="handler",
                recoverability="transient",
            )
            return

        if status == "stored":
            self.ingest_stored_total += 1
            self.last_ingest_error_code = None
        elif status == "duplicate":
            self.ingest_duplicate_total += 1
            self.last_ingest_error_code = None
        elif status == "ingest_failed":
            self.ingest_failed_total += 1
            self.last_ingest_error_code = error_code or "INGEST_FAILED"
        else:
            self.ingest_rejected_total += 1
            self.last_ingest_error_code = error_code or status.upper()

        if status in {"stored", "duplicate"}:
            await self._process_ingested(result)

    async def _process_ingested(self, ingestion_result: Any) -> None:
        raw_message_id = _positive_int_or_none(
            getattr(ingestion_result, "raw_message_id", None)
        )
        if raw_message_id is None:
            self.last_process_at = _utcnow()
            self.last_process_status = "processing_invalid_result"
            self.last_process_error_code = "PROCESSING_INVALID_RESULT"
            self._record_error(
                "PROCESSING_INVALID_RESULT",
                phase="handler",
                recoverability="transient",
            )
            return
        if self.processing_boundary is None:
            return

        self.process_attempt_total += 1
        self.last_process_at = _utcnow()
        try:
            result = await self.processing_boundary.process_raw_message(raw_message_id)
        except Exception:
            self.process_failed_total += 1
            self.last_process_status = "processing_failed"
            self.last_process_error_code = "PROCESSING_BOUNDARY_EXCEPTION"
            self._record_error(
                "PROCESSING_BOUNDARY_EXCEPTION",
                phase="handler",
                recoverability="transient",
            )
            return

        validation_error = _processing_result_error(result, raw_message_id)
        if validation_error is not None:
            self.process_failed_total += 1
            self.last_process_status = "processing_failed"
            self.last_process_error_code = validation_error
            self._record_error(
                validation_error,
                phase="handler",
                recoverability="transient",
            )
            return

        status = result.status
        self.last_process_status = status
        self.last_process_error_code = result.error_code
        if result.parse_executed:
            self.parse_executed_total += 1
        if result.dedup_executed:
            self.dedup_executed_total += 1

        if status in _PROCESSING_SUCCESS_STATUSES:
            self.process_success_total += 1
        elif status == "already_processed":
            self.process_already_done_total += 1
        else:
            self.process_failed_total += 1

    async def _wait_until_stop_or_disconnect(self) -> Literal["stop", "disconnect"]:
        waiters: list[asyncio.Task[Any]] = [
            asyncio.create_task(self._stop_requested.wait()),
        ]
        disconnected = getattr(self.client, "disconnected", None)
        if disconnected is not None:
            waiters.append(asyncio.create_task(_await_external(disconnected)))

        done, pending = await asyncio.wait(
            waiters,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for pending_task in pending:
            pending_task.cancel()
        for done_task in done:
            _consume_task_exception(done_task)

        if self._stop_requested.is_set():
            return "stop"
        return "disconnect"

    async def _reconnect_after_disconnect(self) -> bool:
        self.state = "degraded"
        self.connected = False
        self._record_error(
            "CLIENT_DISCONNECTED",
            phase="runtime",
            recoverability="transient",
        )

        await self._remove_handler()
        self.reconnect_attempt_total += 1
        if (
            self.config.max_reconnect_attempts is not None
            and self.reconnect_attempt_total > self.config.max_reconnect_attempts
        ):
            self._blockers.append("reconnect_exhausted")
            self._record_error(
                "RECONNECT_EXHAUSTED",
                phase="reconnect",
                recoverability="fatal",
            )
            self.state = "failed"
            return False

        delay = self._next_backoff_delay()
        self.backoff_seconds_current = delay
        self._mark_retry_scheduled(delay)
        if delay > 0:
            await self.sleep(delay)
        if self._stop_requested.is_set():
            return False

        try:
            await self._connect_and_register()
        except Exception:
            self._blockers.append("reconnect_failed")
            self.state = "failed"
            return False

        self.reconnect_success_total += 1
        self.last_reconnect_at = _utcnow()
        self.backoff_seconds_current = 0.0
        return True

    def _next_backoff_delay(self) -> float:
        exponent = max(self.reconnect_attempt_total - 1, 0)
        base = self.config.reconnect_initial_delay_seconds * (2**exponent)
        capped = min(base, self.config.reconnect_max_delay_seconds)
        if not self.config.reconnect_jitter or capped == 0:
            return capped
        return random.uniform(0, capped)

    def _mark_retry_scheduled(self, delay: float) -> None:
        if not self._errors:
            return
        latest = self._errors[-1]
        self._errors[-1] = latest.model_copy(
            update={
                "retry_scheduled": "yes",
                "backoff_seconds": delay,
            }
        )

    async def _heartbeat_loop(self) -> None:
        while not self._stop_requested.is_set() and self.state not in {
            "stopped",
            "failed",
        }:
            await self._emit_heartbeat()
            await self.sleep(self.config.heartbeat_interval_seconds)

    async def _emit_heartbeat(self) -> None:
        self.heartbeat_total += 1
        self.last_heartbeat_at = _utcnow()
        heartbeat = MonitorHeartbeat(
            monitor_state=self.state,
            uptime_seconds=self._uptime_seconds(),
            connected=_flag(self.connected),
            handler_registered=_flag(self.handler_registered),
            enabled_source_channels=len(self.watchlist.enabled_source_refs()),
            resolved_channel_count=len(self.resolved_channel_ids),
            events_seen_total=self.events_seen_total,
            events_matched_total=self.events_matched_total,
            events_rejected_total=self.events_rejected_total,
            ingest_attempt_total=self.ingest_attempt_total,
            ingest_stored_total=self.ingest_stored_total,
            ingest_duplicate_total=self.ingest_duplicate_total,
            ingest_rejected_total=self.ingest_rejected_total,
            ingest_failed_total=self.ingest_failed_total,
            last_ingest_at=self.last_ingest_at,
            last_ingest_error_code=self.last_ingest_error_code,
            processing_enabled=_flag(self.processing_boundary is not None),
            process_attempt_total=self.process_attempt_total,
            process_success_total=self.process_success_total,
            process_already_done_total=self.process_already_done_total,
            process_failed_total=self.process_failed_total,
            parse_executed_total=self.parse_executed_total,
            dedup_executed_total=self.dedup_executed_total,
            last_process_at=self.last_process_at,
            last_process_status=self.last_process_status,
            last_process_error_code=self.last_process_error_code,
            conversion_error_total=self.dto_conversion_error_total,
            handler_error_total=self.handler_error_total,
            reconnect_attempt_total=self.reconnect_attempt_total,
            last_event_at=self.last_event_at,
            last_successful_event_at=self.last_successful_event_at,
            last_error_at=self.last_error_at,
            last_error_code=(
                self._errors[-1].error_code if self._errors else None
            ),
            backoff_seconds_current=self.backoff_seconds_current,
            traffic_observed=_flag(self.events_seen_total > 0),
            liveness=self.health().liveness,
            readiness=self.health().readiness,
            heartbeat_total=self.heartbeat_total,
            production_ingest_enabled=_flag(self.ingestion_boundary is not None),
        )
        if self.heartbeat_sink is not None:
            await _maybe_await(self.heartbeat_sink(heartbeat))

    async def _shutdown(self) -> None:
        if self._final_summary_emitted:
            return

        self.shutdown_total += 1
        if self.state not in {"failed", "stopped"}:
            self.state = "draining"
        self._stop_requested.set()

        await self._wait_for_inflight_handlers()
        await self._remove_handler()
        await self._disconnect_client()
        await self._stop_heartbeat()

        if self.state != "failed":
            self.state = "stopped"
        self.stopped_at = _utcnow()
        self._final_summary_emitted = True

    async def _wait_for_inflight_handlers(self) -> None:
        if self.inflight_handler_tasks == 0:
            return
        deadline = asyncio.get_running_loop().time() + self.config.drain_timeout_seconds
        while self.inflight_handler_tasks > 0:
            if asyncio.get_running_loop().time() >= deadline:
                self._blockers.append("drain_timeout")
                self._record_error(
                    "DRAIN_TIMEOUT",
                    phase="shutdown",
                    recoverability="transient",
                )
                return
            await self.sleep(0)

    async def _remove_handler(self) -> None:
        if not self.handler_registered:
            return
        try:
            await _maybe_await(
                self.client.remove_event_handler(
                    self._handler_callback,
                    self._event_builder,
                )
            )
            self.handler_registered = False
            self.handler_removed = True
        except Exception:
            self._blockers.append("handler_remove_failed")
            self._record_error(
                "HANDLER_REMOVE_FAILED",
                phase="shutdown",
                recoverability="fatal",
            )

    async def _disconnect_client(self) -> None:
        try:
            await _maybe_await(self.client.disconnect())
            self.connected = False
            self.client_disconnected_cleanly = True
        except Exception:
            self._blockers.append("disconnect_failed")
            self._record_error(
                "DISCONNECT_FAILED",
                phase="shutdown",
                recoverability="fatal",
            )

    async def _stop_heartbeat(self) -> None:
        if self._heartbeat_task is None:
            return
        self._heartbeat_task.cancel()
        try:
            await self._heartbeat_task
        except asyncio.CancelledError:
            pass

    def _record_error(
        self,
        error_code: RuntimeErrorCode,
        *,
        phase: RuntimeErrorPhase,
        recoverability: Recoverability,
    ) -> None:
        self.last_error_at = _utcnow()
        self._errors.append(
            MonitorRuntimeError(
                error_code=error_code,
                error_phase=phase,
                recoverability=recoverability,
                retry_scheduled="no",
                backoff_seconds=self.backoff_seconds_current,
                occurred_at=self.last_error_at,
            )
        )

    def _build_summary(self) -> MonitorRuntimeSummary:
        if self._summary is not None:
            return self._summary
        self._summary = MonitorRuntimeSummary(
            startup_status=_startup_status(self),
            final_state=self.state,
            uptime_seconds=self._uptime_seconds(),
            enabled_source_channels=len(self.watchlist.enabled_source_refs()),
            resolved_channel_count=len(self.resolved_channel_ids),
            handler_registered_final=_flag(self.handler_registered),
            connected_final=_flag(self.connected),
            events_seen_total=self.events_seen_total,
            events_matched_total=self.events_matched_total,
            events_rejected_total=self.events_rejected_total,
            ingest_attempt_total=self.ingest_attempt_total,
            ingest_stored_total=self.ingest_stored_total,
            ingest_duplicate_total=self.ingest_duplicate_total,
            ingest_rejected_total=self.ingest_rejected_total,
            ingest_failed_total=self.ingest_failed_total,
            last_ingest_at=self.last_ingest_at,
            last_ingest_error_code=self.last_ingest_error_code,
            processing_enabled=_flag(self.processing_boundary is not None),
            process_attempt_total=self.process_attempt_total,
            process_success_total=self.process_success_total,
            process_already_done_total=self.process_already_done_total,
            process_failed_total=self.process_failed_total,
            parse_executed_total=self.parse_executed_total,
            dedup_executed_total=self.dedup_executed_total,
            last_process_at=self.last_process_at,
            last_process_status=self.last_process_status,
            last_process_error_code=self.last_process_error_code,
            dto_conversion_error_total=self.dto_conversion_error_total,
            handler_error_total=self.handler_error_total,
            filter_error_total=self.filter_error_total,
            reconnect_attempt_total=self.reconnect_attempt_total,
            reconnect_success_total=self.reconnect_success_total,
            shutdown_total=self.shutdown_total,
            shutdown_reason=self.shutdown_reason,
            handler_removed=_flag(self.handler_removed),
            client_disconnected_cleanly=_flag(
                self.client_disconnected_cleanly
            ),
            heartbeat_emitted=_flag(self.heartbeat_total > 0),
            production_ingest_enabled=_flag(self.ingestion_boundary is not None),
            blockers=_dedupe_preserve_order(self._blockers),
            errors=list(self._errors),
        )
        return self._summary

    def _uptime_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.stopped_at or _utcnow()
        return max((end - self.started_at).total_seconds(), 0.0)


async def run_monitor_runtime_from_settings(
    *,
    app_settings: Settings | None = None,
    resolver: ChannelResolver | None = None,
    config: MonitorRuntimeConfig | None = None,
    client_factory: MonitorRuntimeClientFactory | None = None,
    heartbeat_sink: HeartbeatSink | None = None,
    event_builder_factory: EventBuilderFactory | None = None,
    ingestion_boundary: IncomingMessageIngestionBoundary | None = None,
    processing_boundary: RawMessageProcessingBoundary | None = None,
) -> MonitorRuntimeSummary:
    """Build and run P6-2D runtime from settings.

    This helper resolves channels once before starting the long-running handler.
    It still does not access DB, parser, normalizer, dedup, bot notification,
    media download, history backfill, or raw event persistence paths.
    """

    resolved_settings = app_settings or settings
    try:
        watchlist = load_watchlist(resolved_settings.WATCHLIST_PATH)
    except FileNotFoundError:
        return _failed_startup_summary("WATCHLIST_UNREADABLE")
    except Exception:
        return _failed_startup_summary("WATCHLIST_SCHEMA_INVALID")

    report = await resolve_source_channels(watchlist, resolver)
    channel_ids = _resolved_channel_ids_from_report(report)
    if report.blockers:
        return _failed_startup_summary(
            "CHANNEL_RESOLUTION_FAILED",
            enabled_source_channels=report.enabled_source_channels,
        )

    runtime = MonitorRuntime(
        watchlist=watchlist,
        client=(client_factory or _settings_client_factory(resolved_settings))(),
        resolved_channel_ids=channel_ids,
        config=config or MonitorRuntimeConfig.from_settings(resolved_settings),
        heartbeat_sink=heartbeat_sink,
        event_builder_factory=event_builder_factory,
        ingestion_boundary=ingestion_boundary,
        processing_boundary=processing_boundary,
    )
    return await runtime.run()


def _settings_client_factory(
    app_settings: Settings,
) -> MonitorRuntimeClientFactory:
    def _factory() -> TelegramClientLike:
        return create_telethon_client_from_settings(app_settings)

    return _factory


def _resolved_channel_ids_from_report(
    report: ResolvedSourceChannelReport,
) -> tuple[int, ...]:
    ids: list[int] = []
    for item in report.results:
        if item.status in {"resolved", "already_numeric"}:
            if item.numeric_channel_id is not None:
                ids.append(item.numeric_channel_id)
    return tuple(ids)


def _positive_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _processing_result_error(
    result: Any,
    expected_raw_message_id: int,
) -> RuntimeErrorCode | None:
    if result is None:
        return "PROCESSING_INVALID_RESULT"
    status = getattr(result, "status", None)
    if status not in _PROCESSING_STATUSES:
        return "PROCESSING_INVALID_RESULT"
    if getattr(result, "raw_message_id", None) != expected_raw_message_id:
        return "PROCESSING_INVALID_RESULT"
    error_code = getattr(result, "error_code", None)
    if error_code is not None and not isinstance(error_code, str):
        return "PROCESSING_INVALID_RESULT"
    if not isinstance(getattr(result, "parse_executed", None), bool):
        return "PROCESSING_INVALID_RESULT"
    if not isinstance(getattr(result, "dedup_executed", None), bool):
        return "PROCESSING_INVALID_RESULT"
    if not isinstance(getattr(result, "eventbus_enabled", None), bool):
        return "PROCESSING_INVALID_RESULT"
    return None


def _failed_startup_summary(
    error_code: RuntimeErrorCode,
    *,
    enabled_source_channels: int = 0,
) -> MonitorRuntimeSummary:
    now = _utcnow()
    return MonitorRuntimeSummary(
        startup_status="fail",
        final_state="failed",
        uptime_seconds=0,
        enabled_source_channels=enabled_source_channels,
        resolved_channel_count=0,
        handler_registered_final="no",
        connected_final="no",
        events_seen_total=0,
        events_matched_total=0,
        events_rejected_total=0,
        ingest_attempt_total=0,
        ingest_stored_total=0,
        ingest_duplicate_total=0,
        ingest_rejected_total=0,
        ingest_failed_total=0,
        last_ingest_at=None,
        last_ingest_error_code=None,
        processing_enabled="no",
        process_attempt_total=0,
        process_success_total=0,
        process_already_done_total=0,
        process_failed_total=0,
        parse_executed_total=0,
        dedup_executed_total=0,
        last_process_at=None,
        last_process_status=None,
        last_process_error_code=None,
        dto_conversion_error_total=0,
        handler_error_total=0,
        filter_error_total=0,
        reconnect_attempt_total=0,
        reconnect_success_total=0,
        shutdown_total=0,
        shutdown_reason="startup_failed",
        handler_removed="no",
        client_disconnected_cleanly="no",
        heartbeat_emitted="no",
        production_ingest_enabled="no",
        blockers=[error_code.casefold()],
        errors=[
            MonitorRuntimeError(
                error_code=error_code,
                error_phase="startup",
                recoverability="fatal",
                retry_scheduled="no",
                backoff_seconds=0,
                occurred_at=now,
            )
        ],
    )


def _build_new_message_event(
    resolved_channel_ids: tuple[int, ...],
    event_builder_factory: EventBuilderFactory | None,
) -> Any:
    if event_builder_factory is not None:
        return event_builder_factory(resolved_channel_ids)

    from telethon import events

    return events.NewMessage(chats=list(resolved_channel_ids))


def _connect_error_code(exc: Exception) -> RuntimeErrorCode:
    message = str(exc).casefold()
    if "api" in message and ("hash" in message or "id" in message):
        return "TELEGRAM_CREDENTIALS_MISSING"
    if "session" in message:
        return "SESSION_UNAVAILABLE"
    if "flood" in message:
        return "FLOOD_WAIT"
    if "forbidden" in message:
        return "ACCESS_FORBIDDEN"
    if "private" in message:
        return "CHANNEL_PRIVATE"
    return "CONNECT_FAILED"


def _startup_status(runtime: MonitorRuntime) -> Literal["pass", "fail"]:
    if runtime.handler_removed or runtime.handler_registered:
        return "pass"
    return "fail"


def _flag(value: bool) -> ReportFlag:
    return "yes" if value else "no"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _await_external(value: Any) -> Any:
    return await asyncio.shield(_maybe_await(value))


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def _maybe_await(value: Any):
    if inspect.isawaitable(value):
        return value

    async def _wrapped():
        return value

    return _wrapped()


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result
