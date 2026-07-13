"""IncomingMessage to RawMessage application ingestion boundary.

P6-2E keeps database access inside this application boundary. Monitor runtime
must not import repositories, sessions, or RawMessageService directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.modules.channel.repository import ChannelRepository
from app.modules.monitor.channel_ids import canonicalize_source_channel_id
from app.modules.monitor.schema import IncomingMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService

IncomingIngestionStatus = Literal[
    "stored",
    "duplicate",
    "channel_not_registered",
    "invalid_source_ref",
    "invalid_message_id",
    "empty_content",
    "ingest_failed",
]
IncomingIngestionErrorCode = Literal[
    "SOURCE_REF_NOT_NUMERIC",
    "SOURCE_REF_OUT_OF_RANGE",
    "SOURCE_REF_NOT_CANONICAL",
    "MESSAGE_ID_NOT_INTEGER",
    "MESSAGE_ID_NON_POSITIVE",
    "CONTENT_EMPTY",
    "CHANNEL_TG_ID_NOT_FOUND",
    "RAW_MESSAGE_INGEST_ERROR",
    "DATABASE_ERROR",
    "INVALID_SERVICE_RESULT",
]
ReportFlag = Literal["yes", "no"]


class _SessionContext(Protocol):
    async def __aenter__(self) -> AsyncSession: ...

    async def __aexit__(self, exc_type, exc, tb) -> object: ...


SessionFactory = Callable[[], _SessionContext]


class _ChannelRepositoryLike(Protocol):
    async def get_by_tg_id(self, tg_id: int) -> Any: ...


class _RawMessageServiceLike(Protocol):
    async def ingest(self, data: RawMessageCreate) -> Any: ...


class IncomingIngestionResult(BaseModel):
    """Desensitized result for one IncomingMessage ingestion attempt."""

    model_config = ConfigDict(frozen=True)

    status: IncomingIngestionStatus
    masked_source_ref: str
    masked_source_message_id: str | None
    raw_message_id: int | None = None
    error_code: IncomingIngestionErrorCode | None = None
    report_desensitized: Literal["yes"] = "yes"
    database_accessed: ReportFlag
    parser_called: Literal["no"] = "no"
    normalizer_called: Literal["no"] = "no"
    dedup_called: Literal["no"] = "no"
    notification_sent: Literal["no"] = "no"
    media_downloaded: Literal["no"] = "no"
    history_backfill_called: Literal["no"] = "no"
    raw_event_persisted: Literal["no"] = "no"
    channel_id_canonical_form: Literal["telethon_marked_peer_id"] = (
        "telethon_marked_peer_id"
    )


class IncomingMessageIngestionBoundary:
    """Application-owned IncomingMessage to RawMessage ingestion boundary."""

    def __init__(
        self,
        *,
        session_factory: SessionFactory = async_session_factory,
        channel_repository_factory: Callable[
            [AsyncSession], _ChannelRepositoryLike
        ] = ChannelRepository,
        raw_message_service_factory: Callable[
            [AsyncSession], _RawMessageServiceLike
        ] = RawMessageService,
    ) -> None:
        self._session_factory = session_factory
        self._channel_repository_factory = channel_repository_factory
        self._raw_message_service_factory = raw_message_service_factory

    async def ingest_incoming(
        self,
        message: IncomingMessage,
    ) -> IncomingIngestionResult:
        """Validate, map, and idempotently ingest one IncomingMessage."""
        source_id = canonicalize_source_channel_id(message.source_ref)
        masked_source_ref = source_id.masked_source_ref
        masked_message_id = _mask_identifier(message.source_message_id)
        if source_id.status != "valid" or source_id.canonical_tg_id is None:
            return _result(
                status="invalid_source_ref",
                masked_source_ref=masked_source_ref,
                masked_source_message_id=masked_message_id,
                error_code=source_id.error_code,
                database_accessed="no",
            )

        message_id = _parse_message_id(message.source_message_id)
        if message_id.error_code is not None or message_id.value is None:
            return _result(
                status="invalid_message_id",
                masked_source_ref=masked_source_ref,
                masked_source_message_id=masked_message_id,
                error_code=message_id.error_code,
                database_accessed="no",
            )

        raw_text = _select_raw_text(message)
        if raw_text is None:
            return _result(
                status="empty_content",
                masked_source_ref=masked_source_ref,
                masked_source_message_id=masked_message_id,
                error_code="CONTENT_EMPTY",
                database_accessed="no",
            )

        async with self._session_factory() as session:
            channel = await self._get_channel(
                session,
                source_id.canonical_tg_id,
                masked_source_ref=masked_source_ref,
                masked_message_id=masked_message_id,
            )
            if isinstance(channel, IncomingIngestionResult):
                return channel
            if channel is None:
                return _result(
                    status="channel_not_registered",
                    masked_source_ref=masked_source_ref,
                    masked_source_message_id=masked_message_id,
                    error_code="CHANNEL_TG_ID_NOT_FOUND",
                    database_accessed="yes",
                )

            return await self._ingest_raw_message(
                session,
                channel_id=int(channel.id),
                source_message_id=message_id.value,
                raw_text=raw_text,
                message=message,
                masked_source_ref=masked_source_ref,
                masked_message_id=masked_message_id,
            )

    async def _get_channel(
        self,
        session: AsyncSession,
        tg_id: int,
        *,
        masked_source_ref: str,
        masked_message_id: str | None,
    ) -> Any | IncomingIngestionResult | None:
        try:
            repo = self._channel_repository_factory(session)
            return await repo.get_by_tg_id(tg_id)
        except Exception:
            await _rollback_quietly(session)
            return _result(
                status="ingest_failed",
                masked_source_ref=masked_source_ref,
                masked_source_message_id=masked_message_id,
                error_code="DATABASE_ERROR",
                database_accessed="yes",
            )

    async def _ingest_raw_message(
        self,
        session: AsyncSession,
        *,
        channel_id: int,
        source_message_id: int,
        raw_text: str,
        message: IncomingMessage,
        masked_source_ref: str,
        masked_message_id: str | None,
    ) -> IncomingIngestionResult:
        data = RawMessageCreate(
            channel_id=channel_id,
            tg_message_id=source_message_id,
            raw_text=raw_text,
            raw_media_refs=message.raw_media_refs,
            raw_payload=message.raw_payload,
            published_at=message.published_at,
        )

        try:
            service = self._raw_message_service_factory(session)
            ingest_result = await service.ingest(data)
            disposition = getattr(ingest_result, "disposition", None)
            raw_message = getattr(ingest_result, "raw_message", ingest_result)
            raw_message_id = getattr(raw_message, "id", None)
            if disposition not in {"stored", "duplicate"}:
                await _rollback_quietly(session)
                return _result(
                    status="ingest_failed",
                    masked_source_ref=masked_source_ref,
                    masked_source_message_id=masked_message_id,
                    error_code="INVALID_SERVICE_RESULT",
                    database_accessed="yes",
                )
            return _result(
                status=disposition,
                masked_source_ref=masked_source_ref,
                masked_source_message_id=masked_message_id,
                raw_message_id=raw_message_id,
                database_accessed="yes",
            )
        except Exception:
            await _rollback_quietly(session)
            return _result(
                status="ingest_failed",
                masked_source_ref=masked_source_ref,
                masked_source_message_id=masked_message_id,
                error_code="RAW_MESSAGE_INGEST_ERROR",
                database_accessed="yes",
            )


class _ParsedMessageId(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: int | None = None
    error_code: Literal[
        "MESSAGE_ID_NOT_INTEGER",
        "MESSAGE_ID_NON_POSITIVE",
    ] | None = None


def _parse_message_id(source_message_id: str | int) -> _ParsedMessageId:
    if isinstance(source_message_id, bool):
        return _ParsedMessageId(error_code="MESSAGE_ID_NOT_INTEGER")
    try:
        value = int(str(source_message_id).strip())
    except ValueError:
        return _ParsedMessageId(error_code="MESSAGE_ID_NOT_INTEGER")
    if value <= 0:
        return _ParsedMessageId(error_code="MESSAGE_ID_NON_POSITIVE")
    return _ParsedMessageId(value=value)


def _select_raw_text(message: IncomingMessage) -> str | None:
    if message.text is not None and message.text.strip():
        return message.text
    if message.caption is not None and message.caption.strip():
        return message.caption
    return None


def _result(
    *,
    status: IncomingIngestionStatus,
    masked_source_ref: str,
    masked_source_message_id: str | None,
    database_accessed: ReportFlag,
    raw_message_id: int | None = None,
    error_code: IncomingIngestionErrorCode | None = None,
) -> IncomingIngestionResult:
    return IncomingIngestionResult(
        status=status,
        masked_source_ref=masked_source_ref,
        masked_source_message_id=masked_source_message_id,
        raw_message_id=raw_message_id,
        error_code=error_code,
        database_accessed=database_accessed,
    )


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        return


def _mask_identifier(value: str | int | None) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return ""
    if len(raw) <= 2:
        return "*" * len(raw)
    if len(raw) <= 6:
        return f"{raw[0]}***{raw[-1]}"
    return f"{raw[:2]}***{raw[-2:]}"
