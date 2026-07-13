"""Unified monitor input DTOs."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class IncomingMessage(BaseModel):
    """One message from any monitor input source."""

    model_config = ConfigDict(frozen=True)

    source_ref: str
    source_message_id: str | int
    source_label: str | None = None
    source_username: str | None = None
    text: str | None = None
    caption: str | None = None
    raw_payload: dict[str, Any] | None = None
    published_at: datetime | None = None

    @property
    def content_text(self) -> str:
        if self.text is not None and self.text.strip():
            return self.text.strip()
        if self.caption is not None and self.caption.strip():
            return self.caption.strip()
        return ""
