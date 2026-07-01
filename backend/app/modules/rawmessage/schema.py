"""RawMessage Pydantic schemas (DTOs)."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class RawMessageCreate(BaseModel):
    channel_id: int
    tg_message_id: int
    raw_text: str = ""
    raw_media_refs: list[dict] | None = None
    raw_payload: dict | None = None
    published_at: datetime | None = None


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
    parsed_data: dict | None
    parser_version: str | None
    rule_version: str | None
    parse_attempts: int
    last_parse_error: str | None
    last_parsed_at: datetime | None
    created_at: datetime
    updated_at: datetime
