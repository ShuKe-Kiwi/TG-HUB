"""Scalar-only Telegram input DTOs."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


class TelegramUpdate(BaseModel):
    """Minimal message update used by the command adapter."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    update_id: int
    message_id: int
    chat_id: int
    text: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "TelegramUpdate":
        """Extract only the four scalar fields needed by P5-D."""
        message = payload.get("message")
        if not isinstance(message, Mapping):
            raise ValueError("Telegram update does not contain a message")

        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            raise ValueError("Telegram message does not contain a chat")

        return cls(
            update_id=payload.get("update_id"),
            message_id=message.get("message_id"),
            chat_id=chat.get("id"),
            text=message.get("text"),
        )
