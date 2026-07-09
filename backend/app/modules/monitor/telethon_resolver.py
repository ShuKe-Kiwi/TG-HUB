"""One-shot Telethon channel resolver adapter.

This module resolves channel identities only. It must not register message
handlers, start long-running loops, download media, or persist any data.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from app.config import Settings, settings
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.resolver import (
    ChannelResolveError,
    ChannelResolveResult,
    ResolvableInputType,
    ResolvedSourceChannelReport,
    resolve_source_channels,
)

PeerIdGetter = Callable[[Any], int]


def _maybe_await(value: Any):
    if inspect.isawaitable(value):
        return value

    async def _wrapped():
        return value

    return _wrapped()


def _extract_username(ref: str, input_type: ResolvableInputType) -> str:
    value = ref.strip()
    if input_type == "username":
        return value[1:] if value.startswith("@") else value

    parsed = urlparse(value)
    parts = [part for part in parsed.path.split("/") if part]
    return parts[0] if parts else value


def _map_exception(exc: Exception) -> ChannelResolveError:
    name = type(exc).__name__
    message = str(exc).casefold()

    if name in {"UsernameNotOccupiedError", "UsernameInvalidError"}:
        return ChannelResolveError("not_found", "USERNAME_NOT_FOUND")
    if name in {"ChannelPrivateError", "UserBannedInChannelError"}:
        return ChannelResolveError("private_or_forbidden", "CHANNEL_PRIVATE")
    if name in {"ForbiddenError", "ChatAdminRequiredError"}:
        return ChannelResolveError("private_or_forbidden", "ACCESS_FORBIDDEN")
    if name == "FloodWaitError":
        return ChannelResolveError("resolver_error", "FLOOD_WAIT")
    if name in {"AuthKeyUnregisteredError", "SessionPasswordNeededError"}:
        return ChannelResolveError("resolver_unavailable", "SESSION_UNAVAILABLE")
    if "not found" in message or "could not find" in message:
        return ChannelResolveError("not_found", "USERNAME_NOT_FOUND")
    if "private" in message or "forbidden" in message:
        return ChannelResolveError("private_or_forbidden", "ACCESS_FORBIDDEN")
    if "connect" in message and "not" in message:
        return ChannelResolveError("resolver_unavailable", "CLIENT_NOT_CONNECTED")

    return ChannelResolveError("resolver_error", "RPC_ERROR")


class TelethonControlledChannelResolver:
    """Resolve channel usernames with a short-lived Telethon client."""

    def __init__(
        self,
        client: Any,
        *,
        get_peer_id: PeerIdGetter,
    ) -> None:
        self._client = client
        self._get_peer_id = get_peer_id

    @classmethod
    def from_settings(
        cls,
        app_settings: Settings | None = None,
    ) -> "TelethonControlledChannelResolver":
        resolved_settings = app_settings or settings
        if resolved_settings.TELEGRAM_API_ID is None:
            raise ChannelResolveError(
                "resolver_unavailable",
                "SESSION_UNAVAILABLE",
            )
        if not resolved_settings.TELEGRAM_API_HASH.strip():
            raise ChannelResolveError(
                "resolver_unavailable",
                "SESSION_UNAVAILABLE",
            )
        if not resolved_settings.TELEGRAM_SESSION_NAME.strip():
            raise ChannelResolveError(
                "resolver_unavailable",
                "SESSION_UNAVAILABLE",
            )

        try:
            from telethon import TelegramClient
            from telethon.utils import get_peer_id
        except ImportError as exc:
            raise ChannelResolveError(
                "resolver_unavailable",
                "SESSION_UNAVAILABLE",
            ) from exc

        client = TelegramClient(
            str(Path(resolved_settings.TELEGRAM_SESSION_NAME).expanduser()),
            resolved_settings.TELEGRAM_API_ID,
            resolved_settings.TELEGRAM_API_HASH,
        )
        return cls(client, get_peer_id=get_peer_id)

    async def connect(self) -> None:
        try:
            await _maybe_await(self._client.connect())
        except Exception as exc:
            raise _map_exception(exc) from exc

    async def disconnect(self) -> None:
        try:
            await _maybe_await(self._client.disconnect())
        except Exception:
            return

    async def __aenter__(self) -> "TelethonControlledChannelResolver":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.disconnect()

    async def resolve(
        self,
        ref: str,
        *,
        input_type: ResolvableInputType,
    ) -> ChannelResolveResult:
        query = _extract_username(ref, input_type)
        try:
            entity = await _maybe_await(self._client.get_entity(query))
            numeric_channel_id = int(self._get_peer_id(entity))
        except Exception as exc:
            raise _map_exception(exc) from exc

        username = getattr(entity, "username", None) or query
        title = getattr(entity, "title", None)
        return ChannelResolveResult(
            input_ref=ref,
            input_type=input_type,
            status="resolved",
            numeric_channel_id=numeric_channel_id,
            username=username,
            title=title,
        )


async def resolve_watchlist_once(
    watchlist: WatchlistConfig,
    resolver: TelethonControlledChannelResolver,
) -> ResolvedSourceChannelReport:
    """Connect, resolve all enabled source channels once, then disconnect."""
    async with resolver:
        report = await resolve_source_channels(watchlist, resolver)
        return report.model_copy(update={"telegram_api_accessed": "yes"})
