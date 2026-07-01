"""Channel Pydantic schemas (DTOs)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ChannelCreate(BaseModel):
    name: str
    tg_id: int
    tg_username: str | None = None
    source_type: str = "telegram"
    status: str = "active"
    rule_profile: str | None = None
    special_parser: str | None = None


class ChannelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    tg_id: int
    tg_username: str | None
    source_type: str
    status: str
    rule_profile: str | None
    special_parser: str | None
    last_message_id: int | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime
