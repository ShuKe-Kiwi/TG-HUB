import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.listener_dry_run import (
    TelethonIncomingMessageAdapter,
    run_monitor_dry_run,
)


@dataclass
class FakeMessage:
    id: int | None
    message: str | None
    date: datetime = datetime(2026, 7, 9, tzinfo=timezone.utc)
    media: Any | None = None
    caption: str | None = None
    grouped_id: int | None = None
    reply_to_msg_id: int | None = None
    edit_date: datetime | None = None


@dataclass
class FakeEvent:
    chat_id: int | None
    message: FakeMessage
    chat: Any | None = None


@dataclass
class FakeNewMessageBuilder:
    chats: tuple[int, ...]


class FakeDryRunClient:
    def __init__(self, *, add_error: Exception | None = None) -> None:
        self.add_error = add_error
        self.connected = False
        self.disconnected_cleanly = False
        self.handlers: list[tuple[Any, Any]] = []
        self.add_calls: list[Any] = []
        self.remove_calls: list[Any] = []
        self.registered = asyncio.Event()
        self.disconnected = asyncio.get_running_loop().create_future()

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected_cleanly = True
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
        self.handlers = [
            item for item in self.handlers if item != (callback, event)
        ]

    async def emit(self, event: FakeEvent) -> None:
        for callback, _event_builder in list(self.handlers):
            await callback(event)

    def force_disconnect(self) -> None:
        if not self.disconnected.done():
            self.disconnected.set_result(None)


def _builder_factory(channel_ids: tuple[int, ...]) -> FakeNewMessageBuilder:
    return FakeNewMessageBuilder(chats=channel_ids)


def _watchlist() -> WatchlistConfig:
    return WatchlistConfig(
        source_channels=[
            {"ref": "https://t.me/demo_channel"},
            {"ref": "https://t.me/another_demo"},
        ],
        watch_titles=[{"title": "家业"}],
    )


def test_adapter_preserves_controlled_photo_evidence() -> None:
    photo = type("Photo", (), {"id": 987654321, "access_hash": "secret"})()
    media = type("MessageMediaPhoto", (), {"photo": photo})()
    message = FakeMessage(
        id=123,
        message="sensitive body",
        media=media,
        grouped_id=456,
        reply_to_msg_id=122,
        edit_date=datetime(2026, 7, 9, 1, 2, tzinfo=timezone.utc),
    )

    incoming = TelethonIncomingMessageAdapter().from_telethon_event(
        FakeEvent(chat_id=-100123, message=message)
    )

    assert incoming.raw_media_refs == [
        {"type": "photo", "telegram_media_id": "987654321"}
    ]
    assert incoming.raw_payload == {
        "schema_version": 1,
        "message_id": "123",
        "channel_id": "-100123",
        "has_media": True,
        "grouped_id": "456",
        "reply_to_message_id": "122",
        "edited_at": "2026-07-09T01:02:00+00:00",
        "media_type": "photo",
    }
    serialized = json.dumps(incoming.raw_payload)
    assert "sensitive body" not in serialized
    assert "secret" not in serialized


def test_adapter_preserves_controlled_document_evidence() -> None:
    document = type(
        "Document",
        (),
        {
            "id": 123456,
            "mime_type": "video/mp4",
            "size": 4096,
            "file_reference": b"secret",
        },
    )()
    media = type("MessageMediaDocument", (), {"document": document})()

    incoming = TelethonIncomingMessageAdapter().from_telethon_event(
        FakeEvent(
            chat_id=-100456,
            message=FakeMessage(id=124, message="title", media=media),
        )
    )

    assert incoming.raw_media_refs == [
        {
            "type": "document",
            "telegram_media_id": "123456",
            "mime_type": "video/mp4",
            "size_bytes": 4096,
        }
    ]
    assert "file_reference" not in json.dumps(incoming.raw_media_refs)


@pytest.mark.asyncio
async def test_no_resolved_channel_ids_exits_without_handler() -> None:
    client = FakeDryRunClient()

    report = await run_monitor_dry_run(
        _watchlist(),
        client,
        resolved_channel_ids=(),
        timeout_seconds=1,
        max_messages=1,
        event_builder_factory=_builder_factory,
    )

    assert report.exit_reason == "setup_error"
    assert report.handler_registered == "no"
    assert report.listener_started == "no"
    assert report.handler_removed == "no"
    assert report.client_disconnected_cleanly == "no"
    assert "empty_resolved_channel_ids" in report.blockers
    assert "handler_not_registered" in report.blockers
    assert client.add_calls == []


@pytest.mark.asyncio
async def test_registers_single_handler_for_all_channels_and_matches_message():
    client = FakeDryRunClient()
    task = asyncio.create_task(
        run_monitor_dry_run(
            _watchlist(),
            client,
            resolved_channel_ids=(-1001, -1002),
            timeout_seconds=5,
            max_messages=1,
            event_builder_factory=_builder_factory,
        )
    )

    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(
            chat_id=-1001,
            message=FakeMessage(
                id=12345,
                message="家业 更新至10集 私密正文 13812345678 https://x.test?a=b",
            ),
        )
    )
    report = await asyncio.wait_for(task, timeout=1)

    assert len(client.add_calls) == 1
    assert client.add_calls[0].chats == (-1001, -1002)
    assert len(client.remove_calls) == 1
    assert report.exit_reason == "max_messages_reached"
    assert report.handler_registered == "yes"
    assert report.handler_removed == "yes"
    assert report.client_disconnected_cleanly == "yes"
    assert report.events_received == 1
    assert report.dto_conversion_success_count == 1
    assert report.filter_pass_count == 1
    assert report.filter_reject_count == 0
    assert report.traffic_observed == "yes"
    assert report.implementation_pass == "yes"
    assert report.allow_P6_2D_design == "yes"
    assert report.database_accessed == "no"
    assert report.parser_called == "no"
    assert report.dedup_called == "no"
    serialized = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    assert "私密正文" not in serialized
    assert "13812345678" not in serialized
    assert "https://x.test" not in serialized
    assert client.disconnected.cancelled() is False


@pytest.mark.asyncio
async def test_filter_reject_is_counted_without_full_text() -> None:
    client = FakeDryRunClient()
    task = asyncio.create_task(
        run_monitor_dry_run(
            _watchlist(),
            client,
            resolved_channel_ids=(-1001,),
            timeout_seconds=5,
            max_messages=1,
            event_builder_factory=_builder_factory,
        )
    )

    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(
            chat_id=-1001,
            message=FakeMessage(id=1, message="完全不相关的内容"),
        )
    )
    report = await asyncio.wait_for(task, timeout=1)

    assert report.filter_pass_count == 0
    assert report.filter_reject_count == 1
    assert report.events[0].filter_result == "rejected"
    assert report.events[0].filter_reason == "no_title_match"


@pytest.mark.asyncio
async def test_out_of_scope_channel_is_dropped_and_counted() -> None:
    client = FakeDryRunClient()
    task = asyncio.create_task(
        run_monitor_dry_run(
            _watchlist(),
            client,
            resolved_channel_ids=(-1001,),
            timeout_seconds=5,
            max_messages=1,
            event_builder_factory=_builder_factory,
        )
    )

    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(
            chat_id=-9999,
            message=FakeMessage(id=1, message="家业"),
        )
    )
    client.force_disconnect()
    report = await asyncio.wait_for(task, timeout=1)

    assert report.out_of_scope_channel_count == 1
    assert report.events_received == 0
    assert report.dto_conversion_success_count == 0
    assert report.events[0].conversion_status == "skipped"
    assert report.events[0].error_code == "OUT_OF_SCOPE_CHANNEL"
    assert report.implementation_pass == "no"


@pytest.mark.asyncio
async def test_dto_conversion_error_is_recorded_and_cleaned_up() -> None:
    client = FakeDryRunClient()
    task = asyncio.create_task(
        run_monitor_dry_run(
            _watchlist(),
            client,
            resolved_channel_ids=(-1001,),
            timeout_seconds=5,
            max_messages=1,
            event_builder_factory=_builder_factory,
        )
    )

    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(
            chat_id=-1001,
            message=FakeMessage(id=None, message="家业"),
        )
    )
    client.force_disconnect()
    report = await asyncio.wait_for(task, timeout=1)

    assert report.dto_conversion_error_count == 1
    assert report.handler_error_count == 1
    assert report.handler_removed == "yes"
    assert report.client_disconnected_cleanly == "yes"
    assert report.events[0].conversion_status == "error"
    assert report.events[0].error_code == "MISSING_MESSAGE_ID"


@pytest.mark.asyncio
async def test_timeout_without_traffic_is_not_implementation_failure() -> None:
    client = FakeDryRunClient()

    report = await run_monitor_dry_run(
        _watchlist(),
        client,
        resolved_channel_ids=(-1001,),
        timeout_seconds=1,
        max_messages=1,
        event_builder_factory=_builder_factory,
    )

    assert report.exit_reason == "timeout"
    assert report.traffic_observed == "no"
    assert report.events_received == 0
    assert report.implementation_pass == "yes"
    assert report.allow_P6_2D_design == "yes"
    assert report.handler_removed == "yes"
    assert report.client_disconnected_cleanly == "yes"


@pytest.mark.asyncio
async def test_client_disconnect_exit_is_clean() -> None:
    client = FakeDryRunClient()
    task = asyncio.create_task(
        run_monitor_dry_run(
            _watchlist(),
            client,
            resolved_channel_ids=(-1001,),
            timeout_seconds=5,
            max_messages=1,
            event_builder_factory=_builder_factory,
        )
    )

    await asyncio.wait_for(client.registered.wait(), timeout=1)
    client.force_disconnect()
    report = await asyncio.wait_for(task, timeout=1)

    assert report.exit_reason == "client_disconnected"
    assert report.handler_removed == "yes"
    assert report.client_disconnected_cleanly == "yes"
    assert report.long_running_process == "no"


@pytest.mark.asyncio
async def test_handler_registration_error_is_setup_error() -> None:
    client = FakeDryRunClient(add_error=RuntimeError("boom"))

    report = await run_monitor_dry_run(
        _watchlist(),
        client,
        resolved_channel_ids=(-1001,),
        timeout_seconds=1,
        max_messages=1,
        event_builder_factory=_builder_factory,
    )

    assert report.exit_reason == "setup_error"
    assert report.handler_registered == "no"
    assert report.handler_removed == "no"
    assert report.client_disconnected_cleanly == "yes"
    assert "setup_error" in report.blockers


@pytest.mark.asyncio
async def test_events_after_exit_are_not_counted() -> None:
    client = FakeDryRunClient()
    task = asyncio.create_task(
        run_monitor_dry_run(
            _watchlist(),
            client,
            resolved_channel_ids=(-1001,),
            timeout_seconds=5,
            max_messages=1,
            event_builder_factory=_builder_factory,
        )
    )

    await asyncio.wait_for(client.registered.wait(), timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=1, message="家业"))
    )
    report = await asyncio.wait_for(task, timeout=1)
    await client.emit(
        FakeEvent(chat_id=-1001, message=FakeMessage(id=2, message="家业"))
    )

    assert report.events_received == 1
    assert len(report.events) == 1
    assert client.handlers == []
