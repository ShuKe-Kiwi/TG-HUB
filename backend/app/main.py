"""FastAPI application factory and MVP-B runtime assembly."""

from contextlib import asynccontextmanager
import inspect

import httpx
from fastapi import FastAPI

from app.application import RawMessageProcessingBoundary
from app.config import Settings, settings
from app.database import async_session_factory
from app.infra.eventbus import InMemoryEventBus
from app.infra.events import ResourceCreated, ResourceMerged
from app.infra.logger import get_logger
from app.infra.logger import setup_logging
from app.modules.bot.handlers import ResourceNotifyHandler
from app.modules.bot.router import (
    SessionFactory,
    TelegramRuntime,
    router as telegram_router,
)
from app.modules.bot.transport import (
    BotTransport,
    TelegramBotTransport,
    TelegramConfigurationError,
)
from app.modules.resource.query_service import ResourceQueryService

setup_logging()

logger = get_logger(__name__)


def _parse_chat_ids(raw_value: str) -> tuple[int, ...]:
    value = raw_value.strip()
    if not value:
        return ()

    chat_ids: list[int] = []
    for raw_chat_id in value.split(","):
        item = raw_chat_id.strip()
        if not item:
            raise ValueError("empty Telegram chat ID")
        try:
            chat_id = int(item)
        except ValueError:
            raise ValueError("invalid Telegram chat ID") from None
        chat_ids.append(chat_id)
    return tuple(dict.fromkeys(chat_ids))


async def _close_if_supported(transport: BotTransport | None) -> None:
    if transport is None:
        return

    close_method = getattr(transport, "aclose", None)
    if close_method is None:
        close_method = getattr(transport, "close", None)
    if close_method is None:
        return

    result = close_method()
    if inspect.isawaitable(result):
        await result


def create_app(
    *,
    app_settings: Settings | None = None,
    bot_transport: BotTransport | None = None,
    session_factory: SessionFactory = async_session_factory,
) -> FastAPI:
    resolved_settings = app_settings or settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        event_bus = InMemoryEventBus()
        app.state.event_bus = event_bus
        app.state.raw_message_processing_boundary = RawMessageProcessingBoundary(
            session_factory=session_factory,
            event_bus=event_bus,
        )

        active_transport = bot_transport
        shutdown_transport = active_transport
        owned_http_client: httpx.AsyncClient | None = None
        telegram_enabled = False
        webhook_secret = ""
        allowed_chat_ids: tuple[int, ...] = ()
        notify_chat_ids: tuple[int, ...] = ()

        try:
            webhook_secret = (
                resolved_settings.TELEGRAM_WEBHOOK_SECRET.strip()
            )
            if not webhook_secret:
                raise TelegramConfigurationError(
                    "Telegram webhook secret is required"
                )

            allowed_chat_ids = _parse_chat_ids(
                resolved_settings.TELEGRAM_ALLOWED_CHAT_IDS
            )
            notify_chat_ids = _parse_chat_ids(
                resolved_settings.TELEGRAM_NOTIFY_CHAT_IDS
            )

            if active_transport is None:
                owned_http_client = httpx.AsyncClient()
                active_transport = TelegramBotTransport.from_env(
                    owned_http_client
                )
                shutdown_transport = active_transport

            telegram_enabled = True
        except (TelegramConfigurationError, ValueError):
            logger.warning(
                "Telegram integration disabled due to missing or invalid "
                "configuration"
            )
            if owned_http_client is not None:
                await owned_http_client.aclose()
                owned_http_client = None
            active_transport = None

        app.state.telegram_runtime = TelegramRuntime(
            enabled=telegram_enabled,
            webhook_secret=webhook_secret,
            allowed_chat_ids=frozenset(allowed_chat_ids),
            transport=active_transport,
            session_factory=session_factory,
        )

        if telegram_enabled and active_transport is not None:
            notify_transport = active_transport

            async def notify_created(event: ResourceCreated) -> None:
                async with session_factory() as session:
                    handler = ResourceNotifyHandler(
                        query_service=ResourceQueryService(session),
                        transport=notify_transport,
                        notify_chat_ids=notify_chat_ids,
                    )
                    await handler.handle_created(event)

            async def notify_merged(event: ResourceMerged) -> None:
                async with session_factory() as session:
                    handler = ResourceNotifyHandler(
                        query_service=ResourceQueryService(session),
                        transport=notify_transport,
                        notify_chat_ids=notify_chat_ids,
                    )
                    await handler.handle_merged(event)

            event_bus.subscribe(ResourceCreated, notify_created)
            event_bus.subscribe(ResourceMerged, notify_merged)

        try:
            yield
        finally:
            await _close_if_supported(shutdown_transport)
            if owned_http_client is not None:
                await owned_http_client.aclose()

    application = FastAPI(
        title=resolved_settings.APP_NAME,
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )
    application.include_router(telegram_router)

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "app": resolved_settings.APP_NAME,
            "env": resolved_settings.APP_ENV,
        }

    return application


app = create_app()
