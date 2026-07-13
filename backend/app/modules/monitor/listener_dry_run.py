"""Short-window Telegram listener dry-run.

P6-2C-2 verifies listener lifecycle only. It must not write to the database,
call parser/dedup/notification code, download media, backfill history, or run as
a long-lived monitor.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.config import Settings, settings
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.filter import WatchlistMatchReason, filter_message
from app.modules.monitor.schema import IncomingMessage

ReportFlag = Literal["yes", "no"]
ReportStatus = Literal["pass", "fail"]
ExitReason = Literal[
    "timeout",
    "max_messages_reached",
    "client_disconnected",
    "setup_error",
    "handler_error",
    "interrupted",
]
FilterResult = Literal["matched", "rejected", "not_run"]
ConversionStatus = Literal["success", "error", "skipped"]


class TelegramClientLike(Protocol):
    def add_event_handler(self, callback: Callable[..., Any], event: Any) -> Any:
        """Register a Telethon-compatible event handler."""

    def remove_event_handler(self, callback: Callable[..., Any], event: Any) -> Any:
        """Remove a Telethon-compatible event handler."""

    async def connect(self) -> Any:
        """Connect the client."""

    async def disconnect(self) -> Any:
        """Disconnect the client."""


class IncomingMessageAdapter(Protocol):
    def from_telethon_event(self, event: object) -> IncomingMessage:
        """Convert one event into the existing monitor input DTO."""


class MonitorDryRunEventReport(BaseModel):
    """Desensitized report item for one observed event."""

    model_config = ConfigDict(frozen=True)

    channel_id_masked: str | None
    message_id_masked: str | None
    received_at: datetime
    has_text: bool
    text_length: int
    text_hash_prefix: str | None
    has_media: bool
    media_type: str | None
    filter_result: FilterResult
    filter_reason: WatchlistMatchReason | None
    conversion_status: ConversionStatus
    error_code: str | None = None


class MonitorDryRunReport(BaseModel):
    """Top-level desensitized P6-2C dry-run report."""

    model_config = ConfigDict(frozen=True)

    watchlist_schema: ReportStatus = "pass"
    enabled_source_channels: int
    resolved_channel_ids: list[str]
    handler_registered: ReportFlag
    listener_started: ReportFlag
    timeout_seconds: int
    max_messages: int
    exit_reason: ExitReason
    implementation_pass: ReportFlag
    traffic_observed: ReportFlag
    events_received: int
    dto_conversion_success_count: int
    dto_conversion_error_count: int
    handler_error_count: int
    filter_pass_count: int
    filter_reject_count: int
    out_of_scope_channel_count: int
    report_desensitized: Literal["yes"] = "yes"
    telegram_api_accessed: Literal["yes"] = "yes"
    database_accessed: Literal["no"] = "no"
    parser_called: Literal["no"] = "no"
    normalizer_called: Literal["no"] = "no"
    dedup_called: Literal["no"] = "no"
    notification_sent: Literal["no"] = "no"
    media_downloaded: Literal["no"] = "no"
    history_backfill_called: Literal["no"] = "no"
    raw_event_persisted: Literal["no"] = "no"
    handler_removed: ReportFlag
    client_disconnected_cleanly: ReportFlag
    long_running_process: Literal["no"] = "no"
    allow_P6_2D_design: ReportFlag
    blockers: list[str]
    events: list[MonitorDryRunEventReport]


class _DryRunAccumulator:
    def __init__(
        self,
        *,
        enabled_source_channels: int,
        resolved_channel_ids: tuple[int, ...],
        timeout_seconds: int,
        max_messages: int,
    ) -> None:
        self.enabled_source_channels = enabled_source_channels
        self.resolved_channel_ids = resolved_channel_ids
        self.timeout_seconds = timeout_seconds
        self.max_messages = max_messages
        self.handler_registered = False
        self.listener_started = False
        self.handler_removed = False
        self.client_disconnected_cleanly = False
        self.exit_reason: ExitReason = "timeout"
        self.events_received = 0
        self.dto_conversion_success_count = 0
        self.dto_conversion_error_count = 0
        self.handler_error_count = 0
        self.filter_pass_count = 0
        self.filter_reject_count = 0
        self.out_of_scope_channel_count = 0
        self.events: list[MonitorDryRunEventReport] = []
        self.blockers: list[str] = []

    def build_report(self) -> MonitorDryRunReport:
        lifecycle_ok = (
            self.handler_registered
            and self.listener_started
            and self.handler_removed
            and self.client_disconnected_cleanly
        )
        error_free = (
            self.dto_conversion_error_count == 0
            and self.handler_error_count == 0
            and self.out_of_scope_channel_count == 0
        )
        implementation_pass = lifecycle_ok and error_free

        blockers = list(self.blockers)
        if not self.resolved_channel_ids:
            blockers.append("empty_resolved_channel_ids")
        if not self.handler_registered:
            blockers.append("handler_not_registered")
        if not self.listener_started:
            blockers.append("listener_not_started")
        if not self.handler_removed:
            blockers.append("handler_not_removed")
        if not self.client_disconnected_cleanly:
            blockers.append("client_not_disconnected_cleanly")
        if self.dto_conversion_error_count > 0:
            blockers.append("dto_conversion_errors")
        if self.handler_error_count > 0:
            blockers.append("handler_errors")
        if self.out_of_scope_channel_count > 0:
            blockers.append("out_of_scope_events")

        return MonitorDryRunReport(
            enabled_source_channels=self.enabled_source_channels,
            resolved_channel_ids=[
                _mask_identifier(value) for value in self.resolved_channel_ids
            ],
            handler_registered=_flag(self.handler_registered),
            listener_started=_flag(self.listener_started),
            timeout_seconds=self.timeout_seconds,
            max_messages=self.max_messages,
            exit_reason=self.exit_reason,
            implementation_pass=_flag(implementation_pass),
            traffic_observed=_flag(self.events_received > 0),
            events_received=self.events_received,
            dto_conversion_success_count=self.dto_conversion_success_count,
            dto_conversion_error_count=self.dto_conversion_error_count,
            handler_error_count=self.handler_error_count,
            filter_pass_count=self.filter_pass_count,
            filter_reject_count=self.filter_reject_count,
            out_of_scope_channel_count=self.out_of_scope_channel_count,
            handler_removed=_flag(self.handler_removed),
            client_disconnected_cleanly=_flag(
                self.client_disconnected_cleanly
            ),
            allow_P6_2D_design=_flag(implementation_pass),
            blockers=_dedupe_preserve_order(blockers),
            events=self.events,
        )


class TelethonIncomingMessageAdapter:
    """Convert a Telethon NewMessage event into the monitor DTO."""

    def from_telethon_event(self, event: object) -> IncomingMessage:
        message = _event_message(event)
        channel_id = _extract_channel_id(event)
        message_id = getattr(message, "id", None) or getattr(event, "id", None)
        if message_id is None:
            raise ValueError("missing_message_id")

        text = (
            getattr(message, "message", None)
            or getattr(event, "raw_text", None)
            or getattr(message, "text", None)
        )
        caption = getattr(message, "caption", None)
        published_at = getattr(message, "date", None) or getattr(
            event,
            "date",
            None,
        )
        chat = getattr(event, "chat", None)
        source_label = getattr(chat, "title", None)
        source_username = getattr(chat, "username", None)

        return IncomingMessage(
            source_ref=str(channel_id),
            source_message_id=message_id,
            source_label=source_label,
            source_username=source_username,
            text=text,
            caption=caption,
            raw_payload=None,
            published_at=published_at,
        )


def create_telethon_client_from_settings(
    app_settings: Settings | None = None,
) -> Any:
    """Create a Telethon client without connecting or registering handlers."""
    resolved_settings = app_settings or settings
    if resolved_settings.TELEGRAM_API_ID is None:
        raise ValueError("TELEGRAM_API_ID is required")
    if not resolved_settings.TELEGRAM_API_HASH.strip():
        raise ValueError("TELEGRAM_API_HASH is required")
    if not resolved_settings.TELEGRAM_SESSION_NAME.strip():
        raise ValueError("TELEGRAM_SESSION_NAME is required")

    from telethon import TelegramClient

    return TelegramClient(
        str(Path(resolved_settings.TELEGRAM_SESSION_NAME).expanduser()),
        resolved_settings.TELEGRAM_API_ID,
        resolved_settings.TELEGRAM_API_HASH,
    )


async def run_monitor_dry_run(
    watchlist: WatchlistConfig,
    client: TelegramClientLike,
    *,
    resolved_channel_ids: tuple[int, ...],
    timeout_seconds: int = 60,
    max_messages: int = 10,
    adapter: IncomingMessageAdapter | None = None,
    event_builder_factory: Callable[[tuple[int, ...]], Any] | None = None,
) -> MonitorDryRunReport:
    """Run a finite NewMessage dry-run and return a desensitized report."""
    accumulator = _DryRunAccumulator(
        enabled_source_channels=len(watchlist.enabled_source_refs()),
        resolved_channel_ids=resolved_channel_ids,
        timeout_seconds=timeout_seconds,
        max_messages=max_messages,
    )
    if not resolved_channel_ids:
        accumulator.exit_reason = "setup_error"
        accumulator.blockers.append("empty_resolved_channel_ids")
        return accumulator.build_report()
    if max_messages <= 0:
        accumulator.exit_reason = "setup_error"
        accumulator.blockers.append("invalid_max_messages")
        return accumulator.build_report()
    if timeout_seconds <= 0:
        accumulator.exit_reason = "setup_error"
        accumulator.blockers.append("invalid_timeout_seconds")
        return accumulator.build_report()

    resolved_id_set = set(resolved_channel_ids)
    done_event = asyncio.Event()
    closing = False
    message_adapter = adapter or TelethonIncomingMessageAdapter()
    event_builder = _build_new_message_event(
        resolved_channel_ids,
        event_builder_factory,
    )

    async def handler(event: object) -> None:
        nonlocal closing
        if closing or done_event.is_set():
            return
        try:
            channel_id = _extract_channel_id(event)
            if channel_id not in resolved_id_set:
                accumulator.out_of_scope_channel_count += 1
                accumulator.events.append(
                    _event_report_for_skipped(event, "OUT_OF_SCOPE_CHANNEL")
                )
                return

            incoming = message_adapter.from_telethon_event(event)
            filter_result = filter_message(incoming, watchlist)
            accumulator.dto_conversion_success_count += 1
            accumulator.events_received += 1
            if filter_result.matched:
                accumulator.filter_pass_count += 1
                report_filter_result: FilterResult = "matched"
            else:
                accumulator.filter_reject_count += 1
                report_filter_result = "rejected"

            accumulator.events.append(
                _event_report_for_message(
                    event,
                    incoming,
                    filter_result=report_filter_result,
                    filter_reason=filter_result.reason,
                )
            )

            if accumulator.events_received >= max_messages:
                accumulator.exit_reason = "max_messages_reached"
                closing = True
                done_event.set()
        except Exception as exc:
            accumulator.dto_conversion_error_count += 1
            accumulator.handler_error_count += 1
            accumulator.events.append(
                _event_report_for_error(event, _stable_error_code(exc))
            )

    handler_callback = handler
    try:
        await _maybe_await(client.connect())
        await _maybe_await(
            client.add_event_handler(handler_callback, event_builder)
        )
        accumulator.handler_registered = True
        accumulator.listener_started = True
        accumulator.exit_reason = await _wait_for_exit(
            client,
            done_event,
            timeout_seconds,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        accumulator.exit_reason = "interrupted"
    except Exception:
        accumulator.exit_reason = "setup_error"
        accumulator.blockers.append("setup_error")
    finally:
        closing = True
        if accumulator.handler_registered:
            try:
                await _maybe_await(
                    client.remove_event_handler(handler_callback, event_builder)
                )
                accumulator.handler_removed = True
            except Exception:
                accumulator.blockers.append("handler_remove_failed")
        try:
            await _maybe_await(client.disconnect())
            accumulator.client_disconnected_cleanly = True
        except Exception:
            accumulator.blockers.append("client_disconnect_failed")

    return accumulator.build_report()


async def _wait_for_exit(
    client: TelegramClientLike,
    done_event: asyncio.Event,
    timeout_seconds: int,
) -> ExitReason:
    waiters: list[asyncio.Task[Any]] = [
        asyncio.create_task(done_event.wait()),
    ]
    disconnected = getattr(client, "disconnected", None)
    if disconnected is not None:
        waiters.append(asyncio.create_task(_await_external(disconnected)))

    done, pending = await asyncio.wait(
        waiters,
        timeout=timeout_seconds,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for pending_task in pending:
        pending_task.cancel()

    if not done:
        return "timeout"
    if done_event.is_set():
        return "max_messages_reached"
    return "client_disconnected"


async def _await_external(value: Any) -> Any:
    return await asyncio.shield(_maybe_await(value))


def _build_new_message_event(
    resolved_channel_ids: tuple[int, ...],
    event_builder_factory: Callable[[tuple[int, ...]], Any] | None,
) -> Any:
    if event_builder_factory is not None:
        return event_builder_factory(resolved_channel_ids)

    from telethon import events

    return events.NewMessage(chats=list(resolved_channel_ids))


def _event_report_for_message(
    event: object,
    incoming: IncomingMessage,
    *,
    filter_result: FilterResult,
    filter_reason: WatchlistMatchReason,
) -> MonitorDryRunEventReport:
    content = incoming.content_text
    return MonitorDryRunEventReport(
        channel_id_masked=_mask_identifier(incoming.source_ref),
        message_id_masked=_mask_identifier(incoming.source_message_id),
        received_at=datetime.now(timezone.utc),
        has_text=bool(content),
        text_length=len(content),
        text_hash_prefix=_hash_prefix(content),
        has_media=_event_has_media(event),
        media_type=_event_media_type(event),
        filter_result=filter_result,
        filter_reason=filter_reason,
        conversion_status="success",
    )


def _event_report_for_skipped(
    event: object,
    error_code: str,
) -> MonitorDryRunEventReport:
    return MonitorDryRunEventReport(
        channel_id_masked=_mask_optional_identifier(_safe_channel_id(event)),
        message_id_masked=_mask_optional_identifier(_safe_message_id(event)),
        received_at=datetime.now(timezone.utc),
        has_text=False,
        text_length=0,
        text_hash_prefix=None,
        has_media=_event_has_media(event),
        media_type=_event_media_type(event),
        filter_result="not_run",
        filter_reason=None,
        conversion_status="skipped",
        error_code=error_code,
    )


def _event_report_for_error(
    event: object,
    error_code: str,
) -> MonitorDryRunEventReport:
    return MonitorDryRunEventReport(
        channel_id_masked=_mask_optional_identifier(_safe_channel_id(event)),
        message_id_masked=_mask_optional_identifier(_safe_message_id(event)),
        received_at=datetime.now(timezone.utc),
        has_text=False,
        text_length=0,
        text_hash_prefix=None,
        has_media=_event_has_media(event),
        media_type=_event_media_type(event),
        filter_result="not_run",
        filter_reason=None,
        conversion_status="error",
        error_code=error_code,
    )


def _event_message(event: object) -> object:
    return getattr(event, "message", event)


def _extract_channel_id(event: object) -> int:
    value = getattr(event, "chat_id", None)
    if value is None:
        value = getattr(_event_message(event), "chat_id", None)
    if value is None:
        value = getattr(event, "peer_id", None)
    if value is None:
        raise ValueError("missing_channel_id")
    return int(value)


def _safe_channel_id(event: object) -> int | None:
    try:
        return _extract_channel_id(event)
    except Exception:
        return None


def _safe_message_id(event: object) -> int | str | None:
    message = _event_message(event)
    return getattr(message, "id", None) or getattr(event, "id", None)


def _event_has_media(event: object) -> bool:
    media = getattr(_event_message(event), "media", None)
    return media is not None


def _event_media_type(event: object) -> str | None:
    media = getattr(_event_message(event), "media", None)
    if media is None:
        return None
    return type(media).__name__


def _hash_prefix(value: str) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _mask_identifier(value: int | str) -> str:
    raw_value = str(value)
    if len(raw_value) <= 2:
        return "*" * len(raw_value)
    if len(raw_value) <= 6:
        return f"{raw_value[0]}***{raw_value[-1]}"
    return f"{raw_value[:2]}***{raw_value[-2:]}"


def _mask_optional_identifier(value: int | str | None) -> str | None:
    if value is None:
        return None
    return _mask_identifier(value)


def _stable_error_code(exc: Exception) -> str:
    message = str(exc).casefold()
    if "missing_channel_id" in message:
        return "MISSING_CHANNEL_ID"
    if "missing_message_id" in message:
        return "MISSING_MESSAGE_ID"
    return "HANDLER_ERROR"


def _flag(value: bool) -> ReportFlag:
    return "yes" if value else "no"


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _maybe_await(value: Any):
    if inspect.isawaitable(value):
        return value

    async def _wrapped():
        return value

    return _wrapped()
