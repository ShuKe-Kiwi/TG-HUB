"""Explicit, bounded Telegram session authorization preflight."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from collections.abc import Callable
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from app.config import Settings
from app.modules.monitor.config import WatchlistConfig, load_watchlist
from app.modules.monitor.preflight import build_static_startup_preflight
from app.modules.monitor.session_ownership import (
    SessionOwnershipError,
    SessionOwnershipLease,
    canonical_session_path,
    validate_session_file,
)
from app.modules.monitor.source_channels import precheck_source_channels

OnlineStatus = Literal["pass", "fail"]
ResolutionStatus = Literal[
    "completed", "partial", "blocked_by_session", "not_started"
]


class OnlineSessionPreflightResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: OnlineStatus
    error_code: str | None = None
    session_authorized: Literal["yes", "no", "unknown"]
    channel_resolution: ResolutionStatus
    enabled_channels: int
    resolved_channels: int
    failed_channels: int
    unattempted_channels: int
    telegram_api_accessed: Literal["yes", "no"]
    database_accessed: Literal["no"] = "no"
    parser_called: Literal["no"] = "no"
    dedup_called: Literal["no"] = "no"
    notification_sent: Literal["no"] = "no"
    listener_started: Literal["no"] = "no"
    report_desensitized: Literal["yes"] = "yes"


class OnlinePreflightClient(Protocol):
    async def connect(self) -> Any: ...
    async def disconnect(self) -> Any: ...
    async def is_user_authorized(self) -> bool: ...
    async def get_input_entity(self, peer: object) -> Any: ...


def _maybe_await(value: Any):
    if inspect.isawaitable(value):
        return value

    async def wrapped():
        return value

    return wrapped()


def _query_for(ref: str, input_type: str) -> object:
    if input_type == "numeric_id":
        return int(ref)
    if input_type == "username":
        return ref[1:] if ref.startswith("@") else ref
    parsed = urlparse(ref)
    return next((part for part in parsed.path.split("/") if part), ref)


def _canonical_peer_id(entity: Any) -> int:
    try:
        from telethon.utils import get_peer_id

        return int(get_peer_id(entity))
    except Exception:
        value = getattr(entity, "channel_id", None)
        if value is None:
            value = getattr(entity, "id", None)
        if value is None:
            raise ValueError("entity has no canonical peer id")
        numeric = int(value)
        return numeric if numeric < 0 else int(f"-100{numeric}")


def create_online_preflight_client(app_settings: Settings) -> OnlinePreflightClient:
    from telethon import TelegramClient

    if app_settings.TELEGRAM_API_ID is None:
        raise RuntimeError("TELEGRAM_API_ID_MISSING")
    return TelegramClient(
        str(canonical_session_path(app_settings)),
        app_settings.TELEGRAM_API_ID,
        app_settings.TELEGRAM_API_HASH,
    )


def _storage_error_code(exc: BaseException) -> str | None:
    code = getattr(exc, "sqlite_errorcode", None)
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return "SESSION_CORRUPTED"
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return "SESSION_STORAGE_BUSY"
    if code in {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CANTOPEN}:
        return "SESSION_PATH_INVALID"
    if isinstance(exc, sqlite3.Error):
        return "SESSION_STORAGE_ERROR"
    return None


class OnlineSessionPreflightService:
    """Sole owner of client tasks, disconnect, and session lease ordering."""

    def __init__(
        self,
        app_settings: Settings,
        *,
        client_factory: Callable[[Settings], OnlinePreflightClient] = (
            create_online_preflight_client
        ),
        lease_factory: Callable[[Settings], SessionOwnershipLease] = (
            SessionOwnershipLease.from_settings
        ),
        static_preflight_builder: Callable[
            [WatchlistConfig, Settings], Any
        ] = build_static_startup_preflight,
        connect_timeout: float = 10,
        authorization_timeout: float = 5,
        channel_timeout: float = 10,
        disconnect_timeout: float = 5,
        overall_timeout: float = 120,
    ) -> None:
        self.settings = app_settings
        self.client_factory = client_factory
        self.lease_factory = lease_factory
        self.static_preflight_builder = static_preflight_builder
        self.connect_timeout = connect_timeout
        self.authorization_timeout = authorization_timeout
        self.channel_timeout = channel_timeout
        self.disconnect_timeout = disconnect_timeout
        self.overall_timeout = overall_timeout

    async def run(self) -> OnlineSessionPreflightResult:
        try:
            loaded = load_watchlist(self.settings.WATCHLIST_PATH)
            snapshot = WatchlistConfig.model_validate(
                loaded.model_dump(mode="python")
            )
        except Exception:
            return self._result(0, error_code="WATCHLIST_UNREADABLE")
        enabled = len(snapshot.enabled_source_refs())
        static = self.static_preflight_builder(snapshot, self.settings)
        if static.status != "pass":
            return self._result(enabled, error_code="STATIC_PREFLIGHT_FAILED")
        try:
            identity = validate_session_file(self.settings)
            lease = self.lease_factory(self.settings)
            lease.acquire()
        except SessionOwnershipError as exc:
            return self._result(enabled, error_code=exc.error_code)
        try:
            lease.verify_identity(self.settings, identity)
            return await self._run_owned(snapshot)
        except asyncio.CancelledError:
            raise
        except SessionOwnershipError as exc:
            return self._result(enabled, error_code=exc.error_code)
        finally:
            lease.release()

    async def _run_owned(
        self, snapshot: WatchlistConfig
    ) -> OnlineSessionPreflightResult:
        enabled = len(snapshot.enabled_source_refs())
        client: OnlinePreflightClient | None = None
        api_accessed: Literal["yes", "no"] = "no"
        try:
            async with asyncio.timeout(self.overall_timeout):
                client = self.client_factory(self.settings)
                await asyncio.wait_for(
                    _maybe_await(client.connect()), self.connect_timeout
                )
                api_accessed = "yes"
                authorized = await asyncio.wait_for(
                    _maybe_await(client.is_user_authorized()),
                    self.authorization_timeout,
                )
                if not authorized:
                    return self._result(
                        enabled,
                        error_code="SESSION_UNAUTHORIZED",
                        authorized="no",
                        api_accessed=api_accessed,
                    )
                resolved = 0
                failed = 0
                refs = snapshot.enabled_source_refs()
                for ref, item in zip(
                    refs, precheck_source_channels(snapshot), strict=True
                ):
                    if item.input_type == "invalid":
                        failed += 1
                        continue
                    try:
                        entity = await asyncio.wait_for(
                            _maybe_await(
                                client.get_input_entity(
                                    _query_for(
                                        ref,
                                        item.input_type,
                                    )
                                )
                            ),
                            self.channel_timeout,
                        )
                        peer_id = _canonical_peer_id(entity)
                        if item.input_type == "numeric_id":
                            expected = item.numeric_channel_id
                            if expected != peer_id:
                                raise ValueError("canonical peer mismatch")
                        resolved += 1
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        failed += 1
                return OnlineSessionPreflightResult(
                    status="pass" if failed == 0 else "fail",
                    error_code=None if failed == 0 else "CHANNEL_RESOLUTION_FAILED",
                    session_authorized="yes",
                    channel_resolution="completed" if failed == 0 else "partial",
                    enabled_channels=enabled,
                    resolved_channels=resolved,
                    failed_channels=failed,
                    unattempted_channels=enabled - resolved - failed,
                    telegram_api_accessed=api_accessed,
                )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            return self._result(
                enabled,
                error_code="TELEGRAM_CONNECT_TIMEOUT",
                api_accessed=api_accessed,
            )
        except Exception as exc:
            return self._result(
                enabled,
                error_code=(
                    _storage_error_code(exc)
                    or (
                        "TELEGRAM_NETWORK_UNAVAILABLE"
                        if isinstance(exc, (ConnectionError, OSError))
                        else "TELEGRAM_RPC_ERROR"
                    )
                ),
                api_accessed=api_accessed,
            )
        finally:
            if client is not None:
                task = asyncio.ensure_future(_maybe_await(client.disconnect()))
                try:
                    await asyncio.shield(
                        asyncio.wait_for(task, self.disconnect_timeout)
                    )
                except asyncio.CancelledError:
                    await task
                    raise
                except Exception:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except (Exception, asyncio.CancelledError):
                            pass

    @staticmethod
    def _result(
        enabled: int,
        *,
        error_code: str,
        authorized: Literal["yes", "no", "unknown"] = "unknown",
        api_accessed: Literal["yes", "no"] = "no",
    ) -> OnlineSessionPreflightResult:
        resolution: ResolutionStatus = (
            "blocked_by_session"
            if error_code.startswith("SESSION_")
            else "not_started"
        )
        return OnlineSessionPreflightResult(
            status="fail",
            error_code=error_code,
            session_authorized=authorized,
            channel_resolution=resolution,
            enabled_channels=enabled,
            resolved_channels=0,
            failed_channels=0,
            unattempted_channels=enabled,
            telegram_api_accessed=api_accessed,
        )


async def run_online_session_preflight(
    app_settings: Settings,
) -> OnlineSessionPreflightResult:
    return await OnlineSessionPreflightService(app_settings).run()
