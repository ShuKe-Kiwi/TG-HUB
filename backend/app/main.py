"""FastAPI application factory and MVP-B runtime assembly."""

from contextlib import asynccontextmanager
import inspect
import secrets

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.application import RawMessageProcessingBoundary
from app.config import Settings, settings
from app.database import async_session_factory
from app.deploy import check_application_readiness, validate_production_config
from app.infra.eventbus import InMemoryEventBus
from app.infra.events import ResourceCreated, ResourceMerged
from app.infra.logger import get_logger
from app.infra.logger import setup_logging
from app.modules.admin import (
    AdminApiError,
    admin_error_handler,
    admin_request_validation_handler,
    guard_admin_request,
    pages_router as admin_pages_router,
    router as admin_router,
    static_directory as admin_static_directory,
)
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
from app.modules.monitor.control import MonitorControlService
from app.modules.monitor.watchlist_service import WatchlistApplicationService

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
    watchlist_service: WatchlistApplicationService | None = None,
    monitor_control_service: MonitorControlService | None = None,
    admin_csrf_token: str | None = None,
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
        active_watchlist_service = (
            watchlist_service or WatchlistApplicationService(resolved_settings)
        )
        active_monitor_control = (
            monitor_control_service
            or MonitorControlService(
                resolved_settings,
                watchlist_service=active_watchlist_service,
            )
        )
        app.state.watchlist_service = active_watchlist_service
        app.state.monitor_control_service = active_monitor_control
        app.state.admin_csrf_token = (
            admin_csrf_token or secrets.token_urlsafe(32)
        )
        app.state.production_config_report = validate_production_config(
            resolved_settings
        )
        app.state.assembly_ready = (
            app.state.production_config_report.status == "pass"
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
                "telegram.integration_disabled",
                extra={"error_code": "TELEGRAM_CONFIG_INVALID"},
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
            await active_monitor_control.shutdown()
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
    application.mount(
        "/admin/static",
        StaticFiles(directory=admin_static_directory),
        name="admin_static",
    )
    application.include_router(admin_pages_router)
    application.include_router(admin_router)
    application.add_exception_handler(AdminApiError, admin_error_handler)
    application.add_exception_handler(
        RequestValidationError,
        admin_request_validation_handler,
    )

    @application.middleware("http")
    async def admin_response_boundary(request: Request, call_next):
        is_admin = (
            request.url.path.startswith("/api/admin/v1")
            or request.url.path == "/admin"
            or request.url.path.startswith("/admin/")
        )
        if is_admin:
            request.state.admin_request_id = secrets.token_hex(12)
            try:
                guard_admin_request(request)
            except AdminApiError as exc:
                response = await admin_error_handler(request, exc)
                response.headers["X-Request-ID"] = (
                    request.state.admin_request_id
                )
                return response
        try:
            response = await call_next(request)
        except Exception:
            if not is_admin:
                raise
            logger.error(
                "admin.request_failed",
                extra={
                    "error_code": "ADMIN_INTERNAL_ERROR",
                    "request_id": request.state.admin_request_id,
                },
            )
            response = await admin_error_handler(
                request,
                AdminApiError("ADMIN_INTERNAL_ERROR", 500),
            )
        if is_admin and response.status_code in {404, 405}:
            response = await admin_error_handler(
                request,
                AdminApiError("INVALID_REQUEST", response.status_code),
            )
        if is_admin:
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Request-ID"] = getattr(
                request.state,
                "admin_request_id",
                "unavailable",
            )
        return response

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "app": resolved_settings.APP_NAME,
            "env": resolved_settings.APP_ENV,
        }

    @application.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok", "service": resolved_settings.APP_NAME}

    @application.get("/health/ready")
    async def health_ready(request: Request) -> JSONResponse:
        control = getattr(request.app.state, "monitor_control_service", None)
        monitor_state = "stopped"
        monitor_error_code = None
        if control is not None:
            try:
                snapshot = await control.status()
                monitor_state = snapshot.control_state
                monitor_error_code = snapshot.last_error_code
            except Exception:
                monitor_state = "unknown"
                monitor_error_code = "MONITOR_STATUS_UNAVAILABLE"
        report = await check_application_readiness(
            resolved_settings,
            session_factory=session_factory,
            assembly_ready=bool(
                getattr(request.app.state, "assembly_ready", False)
            ),
            monitor_state=monitor_state,
            monitor_error_code=monitor_error_code,
        )
        return JSONResponse(
            status_code=200 if report.status == "ready" else 503,
            content=report.model_dump(mode="json"),
            headers={"Cache-Control": "no-store"},
        )

    return application


app = create_app()
