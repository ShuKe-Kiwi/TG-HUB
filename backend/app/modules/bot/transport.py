"""Telegram outbound transport contracts."""

import os
from typing import Protocol

import httpx


class BotTransport(Protocol):
    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
    ) -> None:
        """Send one already-formatted message."""


class TelegramConfigurationError(RuntimeError):
    """Raised when Telegram transport configuration is invalid."""


class TelegramBotTransport:
    """HTTP Telegram transport with an explicitly injected token and client."""

    def __init__(self, token: str, http_client: httpx.AsyncClient) -> None:
        normalized_token = token.strip()
        if not normalized_token:
            raise TelegramConfigurationError("Telegram bot token is required")
        self._token = normalized_token
        self._http_client = http_client

    @classmethod
    def from_env(
        cls,
        http_client: httpx.AsyncClient,
    ) -> "TelegramBotTransport":
        """Build from TELEGRAM_BOT_TOKEN without hidden constructor I/O."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise TelegramConfigurationError(
                "TELEGRAM_BOT_TOKEN is required"
            )
        return cls(token=token, http_client=http_client)

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "HTML",
    ) -> None:
        endpoint = (
            f"https://api.telegram.org/bot{self._token}/sendMessage"
        )
        response = await self._http_client.post(
            endpoint,
            json={
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
            },
        )
        response.raise_for_status()
