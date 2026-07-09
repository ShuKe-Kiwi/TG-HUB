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
    assert summary.handler_removed == "yes"
    assert summary.client_disconnected_cleanly == "yes"
    assert summary.heartbeat_emitted == "yes"
    assert summary.database_accessed == "no"
    assert summary.parser_called == "no"
    assert summary.normalizer_called == "no"
    assert summary.dedup_called == "no"
    assert summary.notification_sent == "no"
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
