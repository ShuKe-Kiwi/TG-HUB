from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application import RawMessageProcessingBoundary
from app.infra.events import DomainEvent, ResourceCreated, ResourceMerged
from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.parser.dto import ParsedLink, ParsedResource
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService
from app.modules.resource.model import Resource, ResourceSource


class RaisingSessionFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError("invalid raw_message_id must not open DB session")


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class FakeService:
    def __init__(
        self,
        raw_message,
        *,
        parse_result=None,
        dedup_result=None,
        parse_error: Exception | None = None,
        dedup_error: Exception | None = None,
    ) -> None:
        self.raw_message = raw_message
        self.parse_result = parse_result
        self.dedup_result = dedup_result
        self.parse_error = parse_error
        self.dedup_error = dedup_error
        self.get_calls = 0
        self.parse_calls = 0
        self.dedup_calls = 0
        self.event_bus_values = []

    async def get_by_id(self, raw_msg_id: int):
        self.get_calls += 1
        return self.raw_message

    async def parse_and_persist(self, raw_msg_id: int):
        self.parse_calls += 1
        if self.parse_error is not None:
            raise self.parse_error
        return self.parse_result if self.parse_result is not None else self.raw_message

    async def dedup_and_persist(self, raw_msg_id: int, event_bus=None):
        self.dedup_calls += 1
        self.event_bus_values.append(event_bus)
        if self.dedup_error is not None:
            raise self.dedup_error
        return self.dedup_result if self.dedup_result is not None else self.raw_message


class RecordingEventBus:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.events: list[DomainEvent] = []
        self.attempts: list[DomainEvent] = []
        self.fail_first = fail_first

    async def publish(self, event: DomainEvent) -> None:
        self.attempts.append(event)
        if self.fail_first and len(self.attempts) == 1:
            raise RuntimeError("simulated event publish failure")
        self.events.append(event)


def _fake_boundary(
    service: FakeService,
    *,
    event_bus=None,
) -> RawMessageProcessingBoundary:
    return RawMessageProcessingBoundary(
        session_factory=lambda: FakeSession(),
        raw_message_service_factory=lambda session: service,
        event_bus=event_bus,
    )


def _fake_raw_message(
    *,
    raw_message_id: int = 1,
    parse_status: str = "parse_pending",
    dedup_status: str = "dedup_pending",
    parsed_data=None,
    parse_attempts: int = 0,
):
    return SimpleNamespace(
        id=raw_message_id,
        parse_status=parse_status,
        dedup_status=dedup_status,
        parsed_data=parsed_data,
        parse_attempts=parse_attempts,
    )


def _session_factory(db_session: AsyncSession):
    assert db_session.bind is not None
    return async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _create_channel(db_session: AsyncSession, tg_id: int):
    return await ChannelService(db_session).create_or_get(
        ChannelCreate(name=f"P6-2F Channel {tg_id}", tg_id=tg_id)
    )


async def _ingest_message(
    db_session: AsyncSession,
    *,
    raw_text: str,
    channel_tg_id: int = 91001,
    tg_message_id: int = 1,
) -> RawMessage:
    channel = await _create_channel(db_session, channel_tg_id)
    result = await RawMessageService(db_session).ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=tg_message_id,
            raw_text=raw_text,
        )
    )
    return result.raw_message


def _parsed_resource(
    *,
    title: str,
    raw_title: str,
    provider: str,
    url: str,
) -> dict:
    return ParsedResource(
        title=title,
        raw_title=raw_title,
        resource_type="drama",
        links=[
            ParsedLink(
                provider=provider,
                original_text=url,
                url=url,
            )
        ],
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_invalid_non_integer_id_does_not_open_session() -> None:
    session_factory = RaisingSessionFactory()
    boundary = RawMessageProcessingBoundary(session_factory=session_factory)

    result = await boundary.process_raw_message("abc")

    assert result.status == "invalid_raw_message_id"
    assert result.error_code == "RAW_MESSAGE_ID_NOT_INTEGER"
    assert result.raw_message_id is None
    assert result.parse_executed is False
    assert result.dedup_executed is False
    assert result.eventbus_enabled is False
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_invalid_bool_id_is_rejected_without_opening_session() -> None:
    session_factory = RaisingSessionFactory()
    boundary = RawMessageProcessingBoundary(session_factory=session_factory)

    result = await boundary.process_raw_message(True)

    assert result.status == "invalid_raw_message_id"
    assert result.error_code == "RAW_MESSAGE_ID_NOT_INTEGER"
    assert result.eventbus_enabled is False
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_invalid_non_positive_id_does_not_open_session() -> None:
    session_factory = RaisingSessionFactory()
    boundary = RawMessageProcessingBoundary(session_factory=session_factory)

    result = await boundary.process_raw_message("0")

    assert result.status == "invalid_raw_message_id"
    assert result.error_code == "RAW_MESSAGE_ID_NON_POSITIVE"
    assert result.eventbus_enabled is False
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_invalid_id_reports_injected_event_bus_without_opening_session() -> None:
    session_factory = RaisingSessionFactory()
    event_bus = RecordingEventBus()
    boundary = RawMessageProcessingBoundary(
        session_factory=session_factory,
        event_bus=event_bus,
    )

    result = await boundary.process_raw_message("abc")

    assert result.status == "invalid_raw_message_id"
    assert result.error_code == "RAW_MESSAGE_ID_NOT_INTEGER"
    assert result.eventbus_enabled is True
    assert event_bus.attempts == []
    assert session_factory.calls == 0


@pytest.mark.asyncio
async def test_strip_string_id_is_allowed(db_session: AsyncSession) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第1集 夸克 https://pan.quark.cn/s/p6f001",
    )
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(f" {raw_message.id} ")

    assert result.raw_message_id == raw_message.id
    assert result.status == "dedup_new"
    assert result.parse_executed is True
    assert result.dedup_executed is True
    assert result.eventbus_enabled is False


@pytest.mark.asyncio
async def test_raw_message_not_found(db_session: AsyncSession) -> None:
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(999999)

    assert result.status == "raw_message_not_found"
    assert result.error_code == "RAW_MESSAGE_NOT_FOUND"
    assert result.raw_message_id == 999999


@pytest.mark.asyncio
async def test_parse_pending_success_runs_parse_and_dedup(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第2集 夸克 https://pan.quark.cn/s/p6f002",
        channel_tg_id=91002,
        tg_message_id=2,
    )
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_new"
    assert result.parse_status == "parsed"
    assert result.dedup_status == "new"
    assert result.parse_attempts == 1
    assert result.parsed_resource_count == 1
    assert result.parse_executed is True
    assert result.dedup_executed is True
    assert result.eventbus_enabled is False

    resources = (await db_session.execute(select(Resource))).scalars().all()
    sources = (await db_session.execute(select(ResourceSource))).scalars().all()
    assert len(resources) == 1
    assert len(sources) == 1


@pytest.mark.asyncio
async def test_parse_pending_no_resource_returns_parse_failed_without_dedup(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="只有标题但没有任何网盘链接",
        channel_tg_id=91003,
        tg_message_id=3,
    )
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "parse_failed"
    assert result.error_code == "RAW_MESSAGE_PARSE_ERROR"
    assert result.parse_status == "parse_failed"
    assert result.dedup_status == "dedup_pending"
    assert result.parse_executed is True
    assert result.dedup_executed is False


@pytest.mark.asyncio
async def test_parsed_pending_runs_only_dedup(db_session: AsyncSession) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第3集 百度 https://pan.baidu.com/s/p6f003 提取码 abc",
        channel_tg_id=91004,
        tg_message_id=4,
    )
    parsed = await RawMessageService(db_session).parse_and_persist(raw_message.id)
    assert parsed.parse_status == "parsed"
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_new"
    assert result.parse_executed is False
    assert result.dedup_executed is True
    assert result.parse_attempts == 1


@pytest.mark.asyncio
async def test_second_serial_call_is_already_processed(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第4集 夸克 https://pan.quark.cn/s/p6f004",
        channel_tg_id=91005,
        tg_message_id=5,
    )
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    first = await boundary.process_raw_message(raw_message.id)
    second = await boundary.process_raw_message(raw_message.id)

    assert first.status == "dedup_new"
    assert second.status == "already_processed"
    assert second.parse_executed is False
    assert second.dedup_executed is False
    assert second.parsed_resource_count == 1


@pytest.mark.asyncio
async def test_second_message_for_same_resource_becomes_dedup_matched(
    db_session: AsyncSession,
) -> None:
    first = await _ingest_message(
        db_session,
        raw_text="【家业】第5集 夸克 https://pan.quark.cn/s/p6f005a",
        channel_tg_id=91006,
        tg_message_id=6,
    )
    second = await _ingest_message(
        db_session,
        raw_text="【家业】第5集 百度 https://pan.baidu.com/s/p6f005b",
        channel_tg_id=91007,
        tg_message_id=7,
    )
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    first_result = await boundary.process_raw_message(first.id)
    second_result = await boundary.process_raw_message(second.id)

    assert first_result.status == "dedup_new"
    assert second_result.status == "dedup_matched"
    assert second_result.dedup_status == "matched"


@pytest.mark.asyncio
async def test_parsed_empty_data_becomes_dedup_skipped(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="manual parsed empty",
        channel_tg_id=91008,
        tg_message_id=8,
    )
    raw_message.parse_status = "parsed"
    raw_message.dedup_status = "dedup_pending"
    raw_message.parsed_data = []
    await db_session.commit()
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_skipped"
    assert result.parse_executed is False
    assert result.dedup_executed is True
    assert result.parsed_resource_count == 0


@pytest.mark.asyncio
async def test_dedup_exception_maps_to_dedup_failed() -> None:
    raw_message = _fake_raw_message(
        parse_status="parsed",
        dedup_status="dedup_pending",
        parsed_data=[{"title": "家业"}],
    )
    service = FakeService(
        raw_message,
        dedup_error=RuntimeError("dedup internals must not leak"),
    )
    boundary = _fake_boundary(service)

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_failed"
    assert result.error_code == "RAW_MESSAGE_DEDUP_ERROR"
    assert result.dedup_executed is True
    assert service.event_bus_values == [None]
    assert result.eventbus_enabled is False


@pytest.mark.asyncio
async def test_injected_event_bus_is_passed_only_to_dedup_path() -> None:
    event_bus = RecordingEventBus()
    raw_message = _fake_raw_message(
        parse_status="parsed",
        dedup_status="dedup_pending",
        parsed_data=[{"title": "家业"}],
    )
    done = _fake_raw_message(
        raw_message_id=raw_message.id,
        parse_status="parsed",
        dedup_status="new",
        parsed_data=raw_message.parsed_data,
    )
    service = FakeService(raw_message, dedup_result=done)
    boundary = _fake_boundary(service, event_bus=event_bus)

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_new"
    assert result.eventbus_enabled is True
    assert service.event_bus_values == [event_bus]


@pytest.mark.asyncio
async def test_service_returning_wrong_raw_message_id_is_invalid() -> None:
    raw_message = _fake_raw_message(parse_status="parsed")
    wrong = _fake_raw_message(
        raw_message_id=raw_message.id + 1,
        parse_status="parsed",
        dedup_status="new",
    )
    service = FakeService(raw_message, dedup_result=wrong)
    boundary = _fake_boundary(service)

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "processing_failed"
    assert result.error_code == "INVALID_SERVICE_RESULT"


@pytest.mark.asyncio
async def test_service_returning_unknown_dedup_status_is_invalid() -> None:
    raw_message = _fake_raw_message(parse_status="parsed")
    unknown = _fake_raw_message(
        raw_message_id=raw_message.id,
        parse_status="parsed",
        dedup_status="strange",
    )
    service = FakeService(raw_message, dedup_result=unknown)
    boundary = _fake_boundary(service)

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "processing_failed"
    assert result.error_code == "INVALID_SERVICE_RESULT"


@pytest.mark.asyncio
async def test_parse_failed_with_final_dedup_status_is_invalid_state() -> None:
    raw_message = _fake_raw_message(
        parse_status="parse_failed",
        dedup_status="new",
    )
    service = FakeService(raw_message)
    boundary = _fake_boundary(service)

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "processing_failed"
    assert result.error_code == "RAW_MESSAGE_INVALID_STATE"
    assert service.parse_calls == 0
    assert service.dedup_calls == 0


@pytest.mark.asyncio
async def test_parse_service_exception_maps_to_parse_failed() -> None:
    raw_message = _fake_raw_message()
    service = FakeService(
        raw_message,
        parse_error=RuntimeError("parser internals must not leak"),
    )
    boundary = _fake_boundary(service)

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "parse_failed"
    assert result.error_code == "RAW_MESSAGE_PARSE_ERROR"
    assert result.parse_executed is True
    assert result.dedup_executed is False


@pytest.mark.asyncio
async def test_dedup_pending_after_dedup_call_maps_to_dedup_failed() -> None:
    raw_message = _fake_raw_message(parse_status="parsed")
    pending = _fake_raw_message(
        raw_message_id=raw_message.id,
        parse_status="parsed",
        dedup_status="dedup_pending",
    )
    service = FakeService(raw_message, dedup_result=pending)
    boundary = _fake_boundary(service)

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_failed"
    assert result.error_code == "RAW_MESSAGE_DEDUP_ERROR"


@pytest.mark.asyncio
async def test_malformed_parsed_data_real_service_maps_to_dedup_failed(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="malformed parsed data",
        channel_tg_id=91009,
        tg_message_id=9,
    )
    raw_message.parse_status = "parsed"
    raw_message.dedup_status = "dedup_pending"
    raw_message.parsed_data = [
        {
            "title": "家业",
            "raw_title": "家业",
            "links": [{"provider": 12345, "original_text": "bad"}],
        }
    ]
    await db_session.commit()
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_failed"
    assert result.error_code == "RAW_MESSAGE_DEDUP_ERROR"
    assert result.parse_status == "parsed"
    assert result.dedup_status == "dedup_pending"


@pytest.mark.asyncio
async def test_multi_resource_message_processes_without_created_count(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="manual multi resource",
        channel_tg_id=91010,
        tg_message_id=10,
    )
    raw_message.parse_status = "parsed"
    raw_message.dedup_status = "dedup_pending"
    raw_message.parsed_data = [
        ParsedResource(
            title="家业",
            raw_title="家业 第1集",
            resource_type="drama",
            links=[
                ParsedLink(
                    provider="quark",
                    original_text="https://pan.quark.cn/s/p6f010a",
                    url="https://pan.quark.cn/s/p6f010a",
                )
            ],
        ).model_dump(mode="json"),
        ParsedResource(
            title="家业",
            raw_title="家业 第2集",
            resource_type="drama",
            links=[
                ParsedLink(
                    provider="baidu",
                    original_text="https://pan.baidu.com/s/p6f010b",
                    url="https://pan.baidu.com/s/p6f010b",
                )
            ],
        ).model_dump(mode="json"),
    ]
    await db_session.commit()
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_new"
    assert result.parsed_resource_count == 2
    assert result.eventbus_enabled is False
    assert not hasattr(result, "resource_created_count")
    assert not hasattr(result, "resource_merged_count")


@pytest.mark.asyncio
async def test_event_bus_publishes_resource_created_from_boundary(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第7集 夸克 https://pan.quark.cn/s/p6g001",
        channel_tg_id=92001,
        tg_message_id=92001,
    )
    event_bus = RecordingEventBus()
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
        event_bus=event_bus,
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_new"
    assert result.eventbus_enabled is True
    assert len(event_bus.events) == 1
    assert isinstance(event_bus.events[0], ResourceCreated)
    assert event_bus.events[0].raw_message_id == raw_message.id


@pytest.mark.asyncio
async def test_one_raw_message_can_publish_multiple_created_events(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="manual multi event",
        channel_tg_id=92002,
        tg_message_id=92002,
    )
    raw_message.parse_status = "parsed"
    raw_message.dedup_status = "dedup_pending"
    raw_message.parsed_data = [
        _parsed_resource(
            title="家业",
            raw_title="家业 第8集",
            provider="quark",
            url="https://pan.quark.cn/s/p6g002a",
        ),
        _parsed_resource(
            title="凡人修仙传",
            raw_title="凡人修仙传 第9集",
            provider="baidu",
            url="https://pan.baidu.com/s/p6g002b",
        ),
    ]
    await db_session.commit()
    event_bus = RecordingEventBus()
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
        event_bus=event_bus,
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_new"
    assert result.parsed_resource_count == 2
    assert len(event_bus.events) == 2
    assert all(isinstance(event, ResourceCreated) for event in event_bus.events)


@pytest.mark.asyncio
async def test_matched_with_new_source_publishes_resource_merged(
    db_session: AsyncSession,
) -> None:
    first = await _ingest_message(
        db_session,
        raw_text="【家业】第10集 夸克 https://pan.quark.cn/s/p6g003a",
        channel_tg_id=92003,
        tg_message_id=92003,
    )
    second = await _ingest_message(
        db_session,
        raw_text="【家业】第10集 百度 https://pan.baidu.com/s/p6g003b",
        channel_tg_id=92004,
        tg_message_id=92004,
    )
    first_boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )
    await first_boundary.process_raw_message(first.id)
    event_bus = RecordingEventBus()
    second_boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
        event_bus=event_bus,
    )

    result = await second_boundary.process_raw_message(second.id)

    assert result.status == "dedup_matched"
    assert result.eventbus_enabled is True
    assert len(event_bus.events) == 1
    assert isinstance(event_bus.events[0], ResourceMerged)
    assert event_bus.events[0].raw_message_id == second.id


@pytest.mark.asyncio
async def test_matched_without_new_source_or_link_publishes_no_event(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第11集 夸克 https://pan.quark.cn/s/p6g004",
        channel_tg_id=92005,
        tg_message_id=92005,
    )
    setup_boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )
    setup = await setup_boundary.process_raw_message(raw_message.id)
    assert setup.status == "dedup_new"

    async with _session_factory(db_session)() as session:
        persisted = await RawMessageService(session).get_by_id(raw_message.id)
        assert persisted is not None
        persisted.dedup_status = "dedup_pending"
        await session.commit()

    event_bus = RecordingEventBus()
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
        event_bus=event_bus,
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_matched"
    assert result.eventbus_enabled is True
    assert event_bus.events == []
    assert event_bus.attempts == []


@pytest.mark.asyncio
async def test_already_processed_does_not_replay_event(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第12集 夸克 https://pan.quark.cn/s/p6g005",
        channel_tg_id=92006,
        tg_message_id=92006,
    )
    setup_boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )
    setup = await setup_boundary.process_raw_message(raw_message.id)
    assert setup.status == "dedup_new"
    event_bus = RecordingEventBus()
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
        event_bus=event_bus,
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "already_processed"
    assert result.eventbus_enabled is True
    assert event_bus.attempts == []


@pytest.mark.asyncio
async def test_event_publish_failure_does_not_rollback_and_continues_events(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="manual publish failure",
        channel_tg_id=92007,
        tg_message_id=92007,
    )
    raw_message.parse_status = "parsed"
    raw_message.dedup_status = "dedup_pending"
    raw_message.parsed_data = [
        _parsed_resource(
            title="家业",
            raw_title="家业 第13集",
            provider="quark",
            url="https://pan.quark.cn/s/p6g006a",
        ),
        _parsed_resource(
            title="凡人修仙传",
            raw_title="凡人修仙传 第14集",
            provider="baidu",
            url="https://pan.baidu.com/s/p6g006b",
        ),
    ]
    await db_session.commit()
    event_bus = RecordingEventBus(fail_first=True)
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
        event_bus=event_bus,
    )

    result = await boundary.process_raw_message(raw_message.id)

    assert result.status == "dedup_new"
    assert result.dedup_status == "new"
    assert result.eventbus_enabled is True
    assert len(event_bus.attempts) == 2
    assert len(event_bus.events) == 1
    async with _session_factory(db_session)() as session:
        persisted = await RawMessageService(session).get_by_id(raw_message.id)
        assert persisted is not None
        assert persisted.dedup_status == "new"


@pytest.mark.asyncio
async def test_result_does_not_expose_runtime_observation_fields(
    db_session: AsyncSession,
) -> None:
    raw_message = await _ingest_message(
        db_session,
        raw_text="【家业】第6集 夸克 https://pan.quark.cn/s/p6f011",
        channel_tg_id=91011,
        tg_message_id=11,
    )
    boundary = RawMessageProcessingBoundary(
        session_factory=_session_factory(db_session),
    )

    result = await boundary.process_raw_message(raw_message.id)
    payload = result.model_dump()

    assert result.status == "dedup_new"
    assert "database_accessed" not in payload
    assert "parser_called" not in payload
    assert "normalizer_called" not in payload
    assert "dedup_called" not in payload
    assert "eventbus_published" not in payload
    assert "event_count" not in payload
    assert "notification_sent" not in payload
    assert "notification_count" not in payload
    assert "media_downloaded" not in payload
    assert payload["eventbus_enabled"] is False
