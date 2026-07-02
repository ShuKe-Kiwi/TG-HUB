"""Telegram webhook route and request-scoped Bot assembly."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import secrets

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.bot.handlers import BotCommandHandler
from app.modules.bot.schema import TelegramUpdate
from app.modules.bot.transport import BotTransport
from app.modules.resource.query_service import ResourceQueryService

SessionFactory = Callable[[], AsyncSession]


@dataclass(frozen=True, slots=True)
class TelegramRuntime:
    enabled: bool
    webhook_secret: str
    allowed_chat_ids: frozenset[int]
    transport: BotTransport | None
    session_factory: SessionFactory


router = APIRouter()


@router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    runtime: TelegramRuntime = request.app.state.telegram_runtime
    if not runtime.enabled or runtime.transport is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Telegram integration is disabled",
        )

    provided_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token",
        "",
    )
    if not provided_secret or not secrets.compare_digest(
        provided_secret,
        runtime.webhook_secret,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid webhook secret",
        )

    try:
        payload = await request.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Telegram update payload must be an object")
        update = TelegramUpdate.from_payload(payload)
    except (TypeError, ValueError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Invalid Telegram update",
        ) from None

    async with runtime.session_factory() as session:
        query_service = ResourceQueryService(session)
        handler = BotCommandHandler(
            query_service=query_service,
            transport=runtime.transport,
            allowed_chat_ids=runtime.allowed_chat_ids,
        )
        await handler.handle(update)

    return {"ok": True}
