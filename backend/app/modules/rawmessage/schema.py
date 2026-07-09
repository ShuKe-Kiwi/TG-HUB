"""RawMessage Pydantic schemas (DTOs)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.modules.rawmessage.model import RawMessage


class RawMessageCreate(BaseModel):
    channel_id: int
    tg_message_id: int
    raw_text: str = ""
    raw_media_refs: list[dict] | None = None
    raw_payload: dict | None = None
    published_at: datetime | None = None


@dataclass
class RawMessageIngestResult:
    """Result of idempotent RawMessage ingest."""

    raw_message: RawMessage
    disposition: Literal["stored", "duplicate"]

    def __getattr__(self, name: str) -> Any:
        """Compatibility proxy for existing call sites that use RawMessage fields."""
        return getattr(self.raw_message, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            name in {"raw_message", "disposition"}
            or "raw_message" not in self.__dict__
        ):
            object.__setattr__(self, name, value)
            return
        setattr(self.raw_message, name, value)


class RawMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    channel_id: int
    tg_message_id: int
    raw_text: str
    raw_media_refs: list[Any] | None
    raw_payload: dict | None
    content_hash: str | None
    published_at: datetime | None
    received_at: datetime
    ingest_status: str
    parse_status: str
    dedup_status: str
    parsed_data: list[dict] | None
    parser_version: str | None
    rule_version: str | None
    parse_attempts: int
    last_parse_error: str | None
    last_parsed_at: datetime | None
    created_at: datetime
    updated_at: datetime
