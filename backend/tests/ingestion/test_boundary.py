from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.ingestion import IncomingMessageIngestionBoundary
from app.modules.monitor.schema import IncomingMessage
from app.modules.rawmessage.model import RawMessage


class RaisingSessionFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("invalid input must not open a DB session")


class RaisingRawMessageService:
    async def ingest(self, data):
        raise RuntimeError("raw exception must not leak")


class InvalidDispositionRawMessageService:
    async def ingest(self, data):
        return SimpleNamespace(
            disposition="unexpected",
            raw_message=SimpleNamespace(id=999),
        )


def _session_factory(db_session: AsyncSession):
    assert db_session.bind is not None
    return async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _create_channel(db_session: AsyncSession, tg_id: int = -1001234567890):
    return await ChannelService(db_session).create_or_get(
        ChannelCreate(
            name="P6-2E Channel",
            tg_id=tg_id,
            tg_username="p6_2e_channel",
        )
    )


@pytest.mark.asyncio
async def test_ingest_incoming_stores_raw_message(db_session: AsyncSession) -> None:
    channel = await _create_channel(db_session)
    boundary = IncomingMessageIngestionBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="-1001234567890",
            source_message_id="1001",
            text="  raw text is preserved  ",
            caption="caption must be ignored",
            raw_media_refs=[
                {"type": "photo", "telegram_media_id": "987654321"}
            ],
            raw_payload={"safe": "stored"},
        )
    )

    assert result.status == "stored"
    assert result.raw_message_id is not None
    assert result.database_accessed == "yes"
    assert result.parser_called == "no"
    assert result.normalizer_called == "no"
    assert result.dedup_called == "no"
    assert result.notification_sent == "no"

    query = select(RawMessage).where(RawMessage.id == result.raw_message_id)
    row = (await db_session.execute(query)).scalar_one()
    assert row.channel_id == channel.id
    assert row.tg_message_id == 1001
    assert row.raw_text == "  raw text is preserved  "
    assert row.raw_payload == {"safe": "stored"}
    assert row.raw_media_refs == [
        {"type": "photo", "telegram_media_id": "987654321"}
    ]


@pytest.mark.asyncio
async def test_ingest_incoming_duplicate_uses_service_disposition(
    db_session: AsyncSession,
) -> None:
    await _create_channel(db_session)
    boundary = IncomingMessageIngestionBoundary(
        session_factory=_session_factory(db_session),
    )
    message = IncomingMessage(
        source_ref="-1001234567890",
        source_message_id=1002,
        text="first",
    )

    first = await boundary.ingest_incoming(message)
    second = await boundary.ingest_incoming(
        message.model_copy(update={"text": "second must not overwrite"})
    )

    assert first.status == "stored"
    assert second.status == "duplicate"
    assert first.raw_message_id == second.raw_message_id

    query = select(RawMessage).where(RawMessage.id == first.raw_message_id)
    row = (await db_session.execute(query)).scalar_one()
    assert row.raw_text == "first"


@pytest.mark.asyncio
async def test_ingest_incoming_uses_caption_when_text_is_blank(
    db_session: AsyncSession,
) -> None:
    await _create_channel(db_session)
    boundary = IncomingMessageIngestionBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="-1001234567890",
            source_message_id=1003,
            text="   ",
            caption="  caption is preserved  ",
        )
    )

    assert result.status == "stored"
    query = select(RawMessage).where(RawMessage.id == result.raw_message_id)
    row = (await db_session.execute(query)).scalar_one()
    assert row.raw_text == "  caption is preserved  "


@pytest.mark.asyncio
async def test_invalid_source_ref_does_not_open_session() -> None:
    session_factory = RaisingSessionFactory()
    boundary = IncomingMessageIngestionBoundary(session_factory=session_factory)

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="1234567890",
            source_message_id=1004,
            text="valid text",
        )
    )

    assert result.status == "invalid_source_ref"
    assert result.error_code == "SOURCE_REF_NOT_CANONICAL"
    assert result.database_accessed == "no"
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_invalid_message_id_does_not_open_session() -> None:
    session_factory = RaisingSessionFactory()
    boundary = IncomingMessageIngestionBoundary(session_factory=session_factory)

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="-1001234567890",
            source_message_id="not-an-int",
            text="valid text",
        )
    )

    assert result.status == "invalid_message_id"
    assert result.error_code == "MESSAGE_ID_NOT_INTEGER"
    assert result.database_accessed == "no"
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_empty_content_does_not_open_session() -> None:
    session_factory = RaisingSessionFactory()
    boundary = IncomingMessageIngestionBoundary(session_factory=session_factory)

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="-1001234567890",
            source_message_id=1005,
            text=" ",
            caption=" ",
        )
    )

    assert result.status == "empty_content"
    assert result.error_code == "CONTENT_EMPTY"
    assert result.database_accessed == "no"
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_channel_not_registered_does_not_call_raw_message_service(
    db_session: AsyncSession,
) -> None:
    raw_service_factory_calls = 0

    def raw_service_factory(session: AsyncSession):
        nonlocal raw_service_factory_calls
        raw_service_factory_calls += 1
        return RaisingRawMessageService()

    boundary = IncomingMessageIngestionBoundary(
        session_factory=_session_factory(db_session),
        raw_message_service_factory=raw_service_factory,
    )

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="-1009876543210",
            source_message_id=1006,
            text="valid text",
        )
    )

    assert result.status == "channel_not_registered"
    assert result.error_code == "CHANNEL_TG_ID_NOT_FOUND"
    assert result.database_accessed == "yes"
    assert raw_service_factory_calls == 0


@pytest.mark.asyncio
async def test_raw_message_service_exception_returns_stable_failure(
    db_session: AsyncSession,
) -> None:
    await _create_channel(db_session)
    boundary = IncomingMessageIngestionBoundary(
        session_factory=_session_factory(db_session),
        raw_message_service_factory=lambda session: RaisingRawMessageService(),
    )

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="-1001234567890",
            source_message_id=1007,
            text="valid text",
        )
    )

    assert result.status == "ingest_failed"
    assert result.error_code == "RAW_MESSAGE_INGEST_ERROR"
    assert result.database_accessed == "yes"
    assert result.raw_message_id is None


@pytest.mark.asyncio
async def test_invalid_service_disposition_returns_stable_failure(
    db_session: AsyncSession,
) -> None:
    await _create_channel(db_session)
    boundary = IncomingMessageIngestionBoundary(
        session_factory=_session_factory(db_session),
        raw_message_service_factory=lambda session: InvalidDispositionRawMessageService(),
    )

    result = await boundary.ingest_incoming(
        IncomingMessage(
            source_ref="-1001234567890",
            source_message_id=1008,
            text="valid text",
        )
    )

    assert result.status == "ingest_failed"
    assert result.error_code == "INVALID_SERVICE_RESULT"
    assert result.raw_message_id is None
