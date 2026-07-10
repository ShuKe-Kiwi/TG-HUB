import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.runtime import (
    MonitorHeartbeat,
    MonitorRuntime,
    MonitorRuntimeConfig,
)


@dataclass
class FakeMessage:
    id: int | None
    message: str | None
    date: datetime = datetime(2026, 7, 9, tzinfo=timezone.utc)
    media: Any | None = None
    caption: str | None = None


@dataclass
class FakeEvent:
    chat_id: int | None
    message: FakeMessage


@dataclass
class FakeNewMessageBuilder:
    chats: tuple[int, ...]


@dataclass
class FakeIngestionResult:
    status: str
    error_code: str | None = None
    raw_message_id: int | None = None


@dataclass
class FakeProcessingResult:
    status: str
    raw_message_id: int | None
    error_code: str | None = None
    parse_executed: bool = False
    dedup_executed: bool = False
    eventbus_enabled: bool = False


class FakeIngestionBoundary:
    def __init__(
        self,
        results: list[FakeIngestionResult] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = results or [FakeIngestionResult("stored")]
        self.error = error
        self.calls: list[Any] = []

    async def ingest_incoming(self, message):
        self.calls.append(message)
        if self.error is not None:
            raise self.error
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


class FakeProcessingBoundary:
    def __init__(
        self,
        results: list[Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.results = results or [
            FakeProcessingResult("dedup_new", raw_message_id=1)
        ]
        self.error = error
        self.calls: list[int] = []

    async def process_raw_message(self, raw_message_id: int):
        self.calls.append(raw_message_id)
        if self.error is not None:
            raise self.error
        if len(self.results) == 1:
            return self.results[0]
        return self.results.pop(0)


class FakeRuntimeClient:
    def __init__(
        self,
        *,
        connect_error: Exception | None = None,
        add_error: Exception | None = None,
        remove_error: Exception | None = None,
        disconnect_error: Exception | None = None,
    ) -> None:
        self.connect_error = connect_error
        self.add_error = add_error
        self.remove_error = remove_error
        self.disconnect_error = disconnect_error
        self.connected = False
        self.handlers: list[tuple[Any, Any]] = []
        self.add_calls: list[Any] = []
        self.remove_calls: list[Any] = []
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.registered = asyncio.Event()
        self.disconnected = asyncio.get_running_loop().create_future()

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True
        if self.disconnected.done():
            self.disconnected = asyncio.get_running_loop().create_future()

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_error is not None:
            raise self.disconnect_error
        self.connected = False
        if not self.disconnected.done():
            self.disconnected.set_result(None)

    def add_event_handler(self, callback, event) -> None:
        if self.add_error is not None:
            raise self.add_error
        self.add_calls.append(event)
        self.handlers.append((callback, event))
        self.registered.set()

    def remove_event_handler(self, callback, event) -> None:
        self.remove_calls.append(event)
        if self.remove_error is not None:
            raise self.remove_error
        self.handlers = [
            item for item in self.handlers if item != (callback, event)
        ]

    async def emit(self, event: FakeEvent) -> None:
        for callback, _event_builder in list(self.handlers):
            await callback(event)

    def force_disconnect(self) -> None:
        self.connected = False
        if not self.disconnected.done():
            self.disconnected.set_result(None)


def _builder_factory(channel_ids: tuple[int, ...]) -> FakeNewMessageBuilder:
    return FakeNewMessageBuilder(chats=channel_ids)


def _runtime_config(**overrides) -> MonitorRuntimeConfig:
    values = {
        "heartbeat_interval_seconds": 999,
        "reconnect_initial_delay_seconds": 0,
        "reconnect_max_delay_seconds": 0,
        "reconnect_stable_reset_seconds": 300,
        "drain_timeout_seconds": 0.1,
        "max_inflight_events": 10,
        "reconnect_jitter": False,
    }
    values.update(overrides)
    return MonitorRuntimeConfig(**values)


def _watchlist() -> WatchlistConfig:
    return WatchlistConfig(
        source_channels=[
            {"ref": "-1001"},
            {"ref": "-1002"},
        ],
        watch_titles=[{"title": "家业"}],
    )


async def _wait_for(predicate, *, timeout: float = 1) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("predicate not satisfied")
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_runtime_registers_single_handler_and_stops_cleanly() -> None:
    client = FakeRuntimeClient()
    heartbeats: list[MonitorHeartbeat] = []
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        heartbeat_sink=heartbeats.append,
        event_builder_factory=_builder_factory,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await asyncio.wait_for(_wait_for(lambda: len(heartbeats) >= 1), timeout=1)
    await client.emit(
        FakeEvent(
            chat_id=-1001,
            message=FakeMessage(
                id=123,
                message="家业 更新至10集 私密正文 13812345678 https://x.test?a=b",
            ),
        )
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert len(client.add_calls) == 1
    assert client.add_calls[0].chats == (-1001, -1002)
    assert len(client.remove_calls) == 1
    assert summary.startup_status == "pass"
    assert summary.final_state == "stopped"
    assert summary.events_seen_total == 1
    assert summary.events_matched_total == 1
    assert summary.events_rejected_total == 0
    assert summary.ingest_attempt_total == 0
    assert summary.production_ingest_enabled == "no"
    assert summary.processing_enabled == "no"
    assert summary.process_attempt_total == 0
    assert summary.process_success_total == 0
    assert summary.process_already_done_total == 0
    assert summary.process_failed_total == 0
    assert summary.parse_executed_total == 0
    assert summary.dedup_executed_total == 0
    assert summary.handler_removed == "yes"
    assert summary.client_disconnected_cleanly == "yes"
    assert summary.heartbeat_emitted == "yes"
    assert summary.history_backfill_called == "no"
    serialized = json.dumps(summary.model_dump(mode="json"), ensure_ascii=False)
    assert "私密正文" not in serialized
    assert "13812345678" not in serialized
    assert "https://x.test" not in serialized


@pytest.mark.asyncio
async def test_runtime_rejects_startup_without_resolved_channels() -> None:
    client = FakeRuntimeClient()
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
    )

    summary = await runtime.run()

    assert summary.startup_status == "fail"
    assert summary.final_state == "failed"
    assert summary.handler_registered_final == "no"
    assert client.add_calls == []
    assert "empty_resolved_channel_ids" in summary.blockers
    assert summary.errors[0].error_code == "CHANNEL_RESOLUTION_FAILED"


@pytest.mark.asyncio
async def test_runtime_handler_registration_failure_disconnects_client() -> None:
    client = FakeRuntimeClient(add_error=RuntimeError("boom"))
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
    )

    summary = await runtime.run()

    assert summary.final_state == "failed"
    assert summary.startup_status == "fail"
    assert summary.client_disconnected_cleanly == "yes"
    assert "handler_registration_failed" in summary.blockers
    assert summary.errors[0].error_code == "HANDLER_REGISTRATION_FAILED"


@pytest.mark.asyncio
async def test_runtime_reconnects_with_one_active_handler() -> None:
    client = FakeRuntimeClient()
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    client.force_disconnect()
    await asyncio.wait_for(_wait_for(lambda: len(client.add_calls) == 2), timeout=1)
    assert len(client.handlers) == 1
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="无关内容"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert summary.final_state == "stopped"
    assert summary.reconnect_attempt_total == 1
    assert summary.reconnect_success_total == 1
    assert len(client.remove_calls) == 2
    assert summary.events_seen_total == 1
    assert summary.events_rejected_total == 1
    assert summary.errors[0].error_code == "CLIENT_DISCONNECTED"
    assert summary.errors[0].retry_scheduled == "yes"


@pytest.mark.asyncio
async def test_runtime_ignores_events_after_stop_requested() -> None:
    client = FakeRuntimeClient()
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    runtime.stop()
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业"))
    )
    summary = await asyncio.wait_for(task, timeout=1)

    assert summary.events_seen_total == 0
    assert summary.events_matched_total == 0
    assert summary.handler_removed == "yes"


@pytest.mark.asyncio
async def test_runtime_records_dto_conversion_error_and_keeps_running() -> None:
    client = FakeRuntimeClient()
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=None, message="家业"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert summary.final_state == "stopped"
    assert summary.dto_conversion_error_total == 1
    assert summary.handler_error_total == 1
    assert summary.errors[0].error_code == "DTO_CONVERSION_ERROR"


@pytest.mark.asyncio
async def test_runtime_handoff_ingests_only_matched_messages() -> None:
    client = FakeRuntimeClient()
    boundary = FakeIngestionBoundary([FakeIngestionResult("stored")])
    heartbeats: list[MonitorHeartbeat] = []
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        heartbeat_sink=heartbeats.append,
        event_builder_factory=_builder_factory,
        ingestion_boundary=boundary,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await asyncio.wait_for(_wait_for(lambda: len(heartbeats) >= 1), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="无关内容"))
    )
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="家业 更新"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert len(boundary.calls) == 1
    assert boundary.calls[0].source_message_id == 2
    assert summary.events_seen_total == 2
    assert summary.events_matched_total == 1
    assert summary.events_rejected_total == 1
    assert summary.ingest_attempt_total == 1
    assert summary.ingest_stored_total == 1
    assert summary.ingest_duplicate_total == 0
    assert summary.ingest_rejected_total == 0
    assert summary.ingest_failed_total == 0
    assert summary.production_ingest_enabled == "yes"
    assert summary.processing_enabled == "no"
    assert summary.process_attempt_total == 0
    assert heartbeats[-1].production_ingest_enabled == "yes"
    assert heartbeats[-1].processing_enabled == "no"


@pytest.mark.asyncio
async def test_runtime_handoff_counts_duplicate_and_rejected_results() -> None:
    client = FakeRuntimeClient()
    boundary = FakeIngestionBoundary(
        [
            FakeIngestionResult("duplicate"),
            FakeIngestionResult(
                "channel_not_registered",
                "CHANNEL_TG_ID_NOT_FOUND",
            ),
        ]
    )
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=boundary,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="家业 B"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert len(boundary.calls) == 2
    assert summary.ingest_attempt_total == 2
    assert summary.ingest_stored_total == 0
    assert summary.ingest_duplicate_total == 1
    assert summary.ingest_rejected_total == 1
    assert summary.ingest_failed_total == 0
    assert summary.last_ingest_error_code == "CHANNEL_TG_ID_NOT_FOUND"


@pytest.mark.asyncio
async def test_runtime_handoff_counts_ingest_failed_without_stopping() -> None:
    client = FakeRuntimeClient()
    boundary = FakeIngestionBoundary(
        [FakeIngestionResult("ingest_failed", "RAW_MESSAGE_INGEST_ERROR")]
    )
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=boundary,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="家业 B"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert summary.final_state == "stopped"
    assert summary.events_matched_total == 2
    assert summary.ingest_attempt_total == 2
    assert summary.ingest_failed_total == 2
    assert summary.handler_error_total == 0
    assert summary.last_ingest_error_code == "RAW_MESSAGE_INGEST_ERROR"


@pytest.mark.asyncio
async def test_runtime_handoff_boundary_exception_does_not_stop_runtime() -> None:
    client = FakeRuntimeClient()
    boundary = FakeIngestionBoundary(error=RuntimeError("hidden internals"))
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=boundary,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="无关内容"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert summary.final_state == "stopped"
    assert summary.events_seen_total == 2
    assert summary.events_matched_total == 1
    assert summary.events_rejected_total == 1
    assert summary.ingest_attempt_total == 1
    assert summary.ingest_failed_total == 1
    assert summary.handler_error_total == 0
    assert summary.errors[0].error_code == "INGESTION_ERROR"
    assert "hidden internals" not in json.dumps(
        summary.model_dump(mode="json"),
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_runtime_processes_stored_and_duplicate_canonical_raw_ids() -> None:
    client = FakeRuntimeClient()
    ingestion = FakeIngestionBoundary(
        [
            FakeIngestionResult("stored", raw_message_id=101),
            FakeIngestionResult("duplicate", raw_message_id=202),
        ]
    )
    processing = FakeProcessingBoundary(
        [
            FakeProcessingResult(
                "dedup_new",
                raw_message_id=101,
                parse_executed=True,
                dedup_executed=True,
                eventbus_enabled=True,
            ),
            FakeProcessingResult(
                "already_processed",
                raw_message_id=202,
                eventbus_enabled=True,
            ),
        ]
    )
    heartbeats: list[MonitorHeartbeat] = []
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        heartbeat_sink=heartbeats.append,
        event_builder_factory=_builder_factory,
        ingestion_boundary=ingestion,
        processing_boundary=processing,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await asyncio.wait_for(_wait_for(lambda: len(heartbeats) >= 1), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="家业 B"))
    )
    await runtime._emit_heartbeat()
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert processing.calls == [101, 202]
    assert summary.ingest_stored_total == 1
    assert summary.ingest_duplicate_total == 1
    assert summary.processing_enabled == "yes"
    assert summary.process_attempt_total == 2
    assert summary.process_success_total == 1
    assert summary.process_already_done_total == 1
    assert summary.process_failed_total == 0
    assert summary.parse_executed_total == 1
    assert summary.dedup_executed_total == 1
    assert summary.last_process_status == "already_processed"
    assert summary.last_process_error_code is None
    assert summary.handler_error_total == 0
    assert (
        summary.process_attempt_total
        == summary.process_success_total
        + summary.process_already_done_total
        + summary.process_failed_total
    )
    assert heartbeats[-1].processing_enabled == "yes"
    assert heartbeats[-1].process_attempt_total == 2
    assert heartbeats[-1].process_success_total == 1
    assert heartbeats[-1].process_already_done_total == 1


@pytest.mark.asyncio
async def test_runtime_skips_processing_for_rejected_ingestion() -> None:
    client = FakeRuntimeClient()
    ingestion = FakeIngestionBoundary(
        [
            FakeIngestionResult(
                "channel_not_registered",
                error_code="CHANNEL_TG_ID_NOT_FOUND",
            )
        ]
    )
    processing = FakeProcessingBoundary()
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=ingestion,
        processing_boundary=processing,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert processing.calls == []
    assert summary.ingest_rejected_total == 1
    assert summary.process_attempt_total == 0
    assert summary.process_failed_total == 0
    assert summary.last_ingest_error_code == "CHANNEL_TG_ID_NOT_FOUND"


@pytest.mark.asyncio
async def test_runtime_rejects_missing_or_invalid_raw_message_id_before_process() -> None:
    client = FakeRuntimeClient()
    ingestion = FakeIngestionBoundary(
        [
            FakeIngestionResult("stored", raw_message_id=None),
            FakeIngestionResult("duplicate", raw_message_id=0),
        ]
    )
    processing = FakeProcessingBoundary()
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=ingestion,
        processing_boundary=processing,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="家业 B"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert processing.calls == []
    assert summary.process_attempt_total == 0
    assert summary.process_failed_total == 0
    assert summary.last_process_error_code == "PROCESSING_INVALID_RESULT"
    assert [error.error_code for error in summary.errors] == [
        "PROCESSING_INVALID_RESULT",
        "PROCESSING_INVALID_RESULT",
    ]


@pytest.mark.asyncio
async def test_runtime_isolates_unknown_ingestion_status() -> None:
    client = FakeRuntimeClient()
    ingestion = FakeIngestionBoundary([FakeIngestionResult("strange")])
    processing = FakeProcessingBoundary()
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=ingestion,
        processing_boundary=processing,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert processing.calls == []
    assert summary.ingest_failed_total == 1
    assert summary.last_ingest_error_code == "INGESTION_INVALID_RESULT"
    assert summary.process_attempt_total == 0
    assert summary.handler_error_total == 0
    assert summary.errors[0].error_code == "INGESTION_INVALID_RESULT"


@pytest.mark.asyncio
async def test_runtime_rejects_invalid_processing_results() -> None:
    client = FakeRuntimeClient()
    ingestion = FakeIngestionBoundary(
        [
            FakeIngestionResult("stored", raw_message_id=301),
            FakeIngestionResult("duplicate", raw_message_id=302),
        ]
    )
    processing = FakeProcessingBoundary(
        [
            FakeProcessingResult("dedup_new", raw_message_id=999),
            FakeProcessingResult("unknown", raw_message_id=302),
        ]
    )
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=ingestion,
        processing_boundary=processing,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="家业 B"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert processing.calls == [301, 302]
    assert summary.process_attempt_total == 2
    assert summary.process_success_total == 0
    assert summary.process_already_done_total == 0
    assert summary.process_failed_total == 2
    assert summary.last_process_error_code == "PROCESSING_INVALID_RESULT"
    assert summary.handler_error_total == 0
    assert [error.error_code for error in summary.errors] == [
        "PROCESSING_INVALID_RESULT",
        "PROCESSING_INVALID_RESULT",
    ]
    assert (
        summary.process_attempt_total
        == summary.process_success_total
        + summary.process_already_done_total
        + summary.process_failed_total
    )


@pytest.mark.asyncio
async def test_runtime_processing_exception_does_not_increment_handler_error() -> None:
    client = FakeRuntimeClient()
    ingestion = FakeIngestionBoundary(
        [FakeIngestionResult("stored", raw_message_id=401)]
    )
    processing = FakeProcessingBoundary(error=RuntimeError("processing exploded"))
    runtime = MonitorRuntime(
        watchlist=_watchlist(),
        client=client,
        resolved_channel_ids=(-1001, -1002),
        config=_runtime_config(),
        event_builder_factory=_builder_factory,
        ingestion_boundary=ingestion,
        processing_boundary=processing,
    )

    task = asyncio.create_task(runtime.run())
    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业 A"))
    )
    runtime.stop()
    summary = await asyncio.wait_for(task, timeout=1)

    assert processing.calls == [401]
    assert summary.process_attempt_total == 1
    assert summary.process_failed_total == 1
    assert summary.handler_error_total == 0
    assert summary.last_process_error_code == "PROCESSING_BOUNDARY_EXCEPTION"
    assert summary.errors[0].error_code == "PROCESSING_BOUNDARY_EXCEPTION"
    assert "processing exploded" not in json.dumps(
        summary.model_dump(mode="json"),
        ensure_ascii=False,
    )
