"""Application boundary DTOs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

RawMessageProcessingStatus = Literal[
    "invalid_raw_message_id",
    "raw_message_not_found",
    "already_processed",
    "parse_failed",
    "dedup_skipped",
    "dedup_new",
    "dedup_matched",
    "dedup_failed",
    "processing_failed",
]

RawMessageProcessingErrorCode = Literal[
    "RAW_MESSAGE_ID_NOT_INTEGER",
    "RAW_MESSAGE_ID_NON_POSITIVE",
    "RAW_MESSAGE_NOT_FOUND",
    "RAW_MESSAGE_INVALID_STATE",
    "RAW_MESSAGE_PARSE_ERROR",
    "RAW_MESSAGE_DEDUP_ERROR",
    "RAW_MESSAGE_PROCESSING_ERROR",
    "INVALID_SERVICE_RESULT",
    "DATABASE_ERROR",
]


class RawMessageProcessingResult(BaseModel):
    """Stable result for RawMessage processing."""

    model_config = ConfigDict(frozen=True)

    status: RawMessageProcessingStatus
    raw_message_id: int | None
    parse_status: str | None = None
    dedup_status: str | None = None
    parse_attempts: int | None = None
    parsed_resource_count: int = Field(default=0, ge=0)
    error_code: RawMessageProcessingErrorCode | None = None
    parse_executed: bool = False
    dedup_executed: bool = False
    eventbus_enabled: bool = False
