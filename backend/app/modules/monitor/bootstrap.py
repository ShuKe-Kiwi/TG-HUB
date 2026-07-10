"""Composition root for the production monitor command."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.application import RawMessageProcessingBoundary
from app.config import Settings, settings
from app.database import async_session_factory
from app.infra.eventbus import InMemoryEventBus
from app.infra.events import ResourceCreated, ResourceMerged
from app.modules.bot.handlers import ResourceNotifyHandler
from app.modules.bot.transport import TelegramBotTransport
from app.modules.ingestion import IncomingMessageIngestionBoundary
from app.modules.monitor.config import load_watchlist
from app.modules.monitor.heartbeat import HeartbeatSink, NullHeartbeatSink
from app.modules.monitor.resolver import resolve_source_channels
from app.modules.monitor.runtime import (
    MonitorRuntime,
    MonitorRuntimeConfig,
    MonitorRuntimeSummary,
    failed_monitor_runtime_summary,
)
from app.modules.monitor.listener_dry_run import (
    create_telethon_client_from_settings,
)
from app.modules.monitor.telethon_resolver import (
    TelethonControlledChannelResolver,
)
from app.modules.resource.query_service import ResourceQueryService

BotNotificationStatus = Literal[
    "enabled",
    "disabled_by_flag",
    "disabled_config_missing",
]


class MonitorAssemblyReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    database_ready: Literal["yes", "no"]
    ingestion_boundary_ready: Literal["yes"] = "yes"
    processing_boundary_ready: Literal["yes"] = "yes"
    event_bus_ready: Literal["yes"] = "yes"
    bot_notification_status: BotNotificationStatus
    heartbeat_status: Literal["enabled", "disabled", "write_failed"]
    blockers: list[str]


class MonitorBootstrapResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    assembly: MonitorAssemblyReport
    runtime: MonitorRuntimeSummary


def _parse_chat_ids(raw_value: str) -> tuple[int, ...]:
    if not raw_value.strip():
        return ()
    values: list[int] = []
    for raw_item in raw_value.split(","):
        try:
            value = int(raw_item.strip())
        except ValueError:
            return ()
        values.append(value)
    return tuple(dict.fromkeys(values))


class MonitorBootstrap:
    """Own assembly resources while MonitorRuntime owns its lifecycle."""

    def __init__(
        self,
        app_settings: Settings | None = None,
        *,
        heartbeat_sink: HeartbeatSink | None = None,
        no_bot_notify: bool = False,
        session_factory: Callable[[], Any] = async_session_factory,
        environment: Mapping[str, str] | None = None,
        http_client_factory: Callable[[], httpx.AsyncClient] = httpx.AsyncClient,
        resolver_factory: Callable[[Settings], Any] | None = None,
        client_factory: Callable[[Settings], Any] = create_telethon_client_from_settings,
        runtime_factory: Callable[..., MonitorRuntime] = MonitorRuntime,
    ) -> None:
        self.settings = app_settings or settings
        self.heartbeat_sink = heartbeat_sink or NullHeartbeatSink()
        self.no_bot_notify = no_bot_notify
        self.session_factory = session_factory
        self.environment = environment if environment is not None else os.environ
        self.http_client_factory = http_client_factory
        self.resolver_factory = resolver_factory or (
            lambda configured: TelethonControlledChannelResolver.from_settings(
                configured
            )
        )
        self.client_factory = client_factory
        self.runtime_factory = runtime_factory
        self.runtime: MonitorRuntime | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._stop_requested = False

    def stop(self) -> None:
        self._stop_requested = True
        if self.runtime is not None:
            self.runtime.stop()

    async def run(self) -> MonitorBootstrapResult:
        database_ready = await self._check_database()
        if not database_ready:
            return self._result(
                failed_monitor_runtime_summary("CONNECT_FAILED"),
                database_ready=False,
                bot_status=self._bot_status(),
                blockers=["database_unavailable"],
            )

        event_bus = InMemoryEventBus()
        ingestion = IncomingMessageIngestionBoundary(
            session_factory=self.session_factory
        )
        processing = RawMessageProcessingBoundary(
            session_factory=self.session_factory,
            event_bus=event_bus,
        )
        bot_status = await self._configure_notifications(event_bus)

        watchlist = load_watchlist(self.settings.WATCHLIST_PATH)
        resolver = self.resolver_factory(self.settings)
        async with resolver:
            resolution = await resolve_source_channels(watchlist, resolver)
        channel_ids = tuple(
            item.numeric_channel_id
            for item in resolution.results
            if item.status in {"resolved", "already_numeric"}
            and item.numeric_channel_id is not None
        )
        if resolution.blockers:
            return self._result(
                failed_monitor_runtime_summary(
                    "CHANNEL_RESOLUTION_FAILED",
                    enabled_source_channels=resolution.enabled_source_channels,
                ),
                database_ready=True,
                bot_status=bot_status,
                blockers=list(resolution.blockers),
            )

        self.runtime = self.runtime_factory(
            watchlist=watchlist,
            client=self.client_factory(self.settings),
            resolved_channel_ids=channel_ids,
            config=MonitorRuntimeConfig.from_settings(self.settings),
            heartbeat_sink=self.heartbeat_sink,
            ingestion_boundary=ingestion,
            processing_boundary=processing,
        )
        if self._stop_requested:
            self.runtime.stop()
        summary = await self.runtime.run()
        return self._result(
            summary,
            database_ready=True,
            bot_status=bot_status,
            blockers=[],
        )

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None
        await self.heartbeat_sink.aclose()

    async def _check_database(self) -> bool:
        try:
            await asyncio.wait_for(self._execute_database_probe(), timeout=5.0)
            return True
        except Exception:
            return False

    async def _execute_database_probe(self) -> None:
        async with self.session_factory() as session:
            await session.execute(text("SELECT 1"))

    def _bot_status(self) -> BotNotificationStatus:
        if self.no_bot_notify:
            return "disabled_by_flag"
        token = self._bot_token()
        chat_ids = _parse_chat_ids(self.settings.TELEGRAM_NOTIFY_CHAT_IDS)
        return "enabled" if token and chat_ids else "disabled_config_missing"

    async def _configure_notifications(
        self,
        event_bus: InMemoryEventBus,
    ) -> BotNotificationStatus:
        status = self._bot_status()
        if status != "enabled":
            return status

        token = self._bot_token()
        chat_ids = _parse_chat_ids(self.settings.TELEGRAM_NOTIFY_CHAT_IDS)
        self._http_client = self.http_client_factory()
        transport = TelegramBotTransport(token, self._http_client)
        session_factory = self.session_factory

        async def notify_created(event: ResourceCreated) -> None:
            async with session_factory() as session:
                handler = ResourceNotifyHandler(
                    ResourceQueryService(session), transport, chat_ids
                )
                await handler.handle_created(event)

        async def notify_merged(event: ResourceMerged) -> None:
            async with session_factory() as session:
                handler = ResourceNotifyHandler(
                    ResourceQueryService(session), transport, chat_ids
                )
                await handler.handle_merged(event)

        event_bus.subscribe(ResourceCreated, notify_created)
        event_bus.subscribe(ResourceMerged, notify_merged)
        return status

    def _bot_token(self) -> str:
        return (
            self.environment.get("TELEGRAM_BOT_TOKEN", "").strip()
            or self.settings.TELEGRAM_BOT_TOKEN.strip()
        )

    def _result(
        self,
        summary: MonitorRuntimeSummary,
        *,
        database_ready: bool,
        bot_status: BotNotificationStatus,
        blockers: list[str],
    ) -> MonitorBootstrapResult:
        heartbeat_status: Literal["enabled", "disabled", "write_failed"]
        if getattr(self.heartbeat_sink, "error_count", 0):
            heartbeat_status = "write_failed"
        elif isinstance(self.heartbeat_sink, NullHeartbeatSink):
            heartbeat_status = "disabled"
        else:
            heartbeat_status = "enabled"
        return MonitorBootstrapResult(
            assembly=MonitorAssemblyReport(
                database_ready="yes" if database_ready else "no",
                bot_notification_status=bot_status,
                heartbeat_status=heartbeat_status,
                blockers=blockers,
            ),
            runtime=summary,
        )
