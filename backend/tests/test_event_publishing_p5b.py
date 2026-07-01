"""P5-B tests for resource events published after database commit."""

from collections.abc import MutableSequence

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infra.eventbus import InMemoryEventBus
from app.infra.events import DomainEvent, ResourceCreated, ResourceMerged
from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService
from app.modules.resource.model import Resource, ResourceLink, ResourceSource, Work


async def _ingest_and_parse(
    db_session: AsyncSession,
    *,
    raw_text: str,
    tg_id: int,
) -> RawMessage:
    channel = await ChannelService(db_session).create_or_get(
        ChannelCreate(name=f"P5-B 测试频道 {tg_id}", tg_id=tg_id),
    )
    message = await RawMessageService(db_session).ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=tg_id,
            raw_text=raw_text,
        )
    )
    return await RawMessageService(db_session).parse_and_persist(message.id)


def _recording_bus(
    events: MutableSequence[DomainEvent],
) -> InMemoryEventBus:
    event_bus = InMemoryEventBus()

    async def capture(event: DomainEvent) -> None:
        events.append(event)

    event_bus.subscribe(ResourceCreated, capture)
    event_bus.subscribe(ResourceMerged, capture)
    return event_bus


async def _count(db_session: AsyncSession, model: type) -> int:
    result = await db_session.execute(select(func.count(model.id)))
    return result.scalar_one()


@pytest.mark.asyncio
async def test_create_path_publishes_resource_created_after_commit(
    db_session: AsyncSession,
) -> None:
    message = await _ingest_and_parse(
        db_session,
        raw_text=(
            "【家业】第1集 夸克 "
            "https://pan.quark.cn/s/p5b-create 提取码 abc"
        ),
        tg_id=75101,
    )
    events: list[DomainEvent] = []
    event_bus = InMemoryEventBus()

    async def verify_committed(event: ResourceCreated) -> None:
        async with AsyncSession(bind=db_session.bind) as observer:
            resource = await observer.get(Resource, event.resource_id)
            persisted_message = await observer.get(RawMessage, message.id)
            assert resource is not None
            assert persisted_message is not None
            assert persisted_message.dedup_status == "new"
        events.append(event)

    event_bus.subscribe(ResourceCreated, verify_committed)

    result = await RawMessageService(db_session).dedup_and_persist(
        message.id,
        event_bus=event_bus,
    )

    assert result.dedup_status == "new"
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ResourceCreated)
    assert event.resource_id > 0
    assert event.work_id > 0
    assert event.raw_message_id == message.id
    assert event.source_count == 1


@pytest.mark.asyncio
async def test_merge_path_publishes_resource_merged(
    db_session: AsyncSession,
) -> None:
    first = await _ingest_and_parse(
        db_session,
        raw_text=(
            "【家业】第1集 夸克 "
            "https://pan.quark.cn/s/p5b-merge-a 提取码 abc"
        ),
        tg_id=75201,
    )
    await RawMessageService(db_session).dedup_and_persist(first.id)

    second = await _ingest_and_parse(
        db_session,
        raw_text=(
            "【家业】第1集 百度 "
            "https://pan.baidu.com/s/p5b-merge-b 提取码 def"
        ),
        tg_id=75202,
    )
    events: list[DomainEvent] = []

    result = await RawMessageService(db_session).dedup_and_persist(
        second.id,
        event_bus=_recording_bus(events),
    )

    assert result.dedup_status == "matched"
    assert len(events) == 1
    event = events[0]
    assert isinstance(event, ResourceMerged)
    assert event.raw_message_id == second.id
    assert event.source_count == 2
    assert event.created_source is True
    assert event.created_link_count == 1


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_publish_event(
    db_session: AsyncSession,
) -> None:
    message = await _ingest_and_parse(
        db_session,
        raw_text=(
            "【家业】第1集 夸克 "
            "https://pan.quark.cn/s/p5b-idempotent 提取码 abc"
        ),
        tg_id=75301,
    )
    events: list[DomainEvent] = []
    event_bus = _recording_bus(events)

    first = await RawMessageService(db_session).dedup_and_persist(
        message.id,
        event_bus=event_bus,
    )
    assert first.dedup_status == "new"
    assert len(events) == 1
    events.clear()

    replay = await RawMessageService(db_session).dedup_and_persist(
        message.id,
        event_bus=event_bus,
    )

    assert replay.dedup_status == "matched"
    assert events == []


@pytest.mark.asyncio
async def test_skipped_message_does_not_publish_resource_event(
    db_session: AsyncSession,
) -> None:
    message = await _ingest_and_parse(
        db_session,
        raw_text="纯文本消息没有任何资源链接",
        tg_id=75401,
    )
    events: list[DomainEvent] = []

    result = await RawMessageService(db_session).dedup_and_persist(
        message.id,
        event_bus=_recording_bus(events),
    )

    assert result.dedup_status == "skipped"
    assert events == []


@pytest.mark.asyncio
async def test_commit_failure_does_not_publish_event(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = await _ingest_and_parse(
        db_session,
        raw_text=(
            "【家业】第1集 夸克 "
            "https://pan.quark.cn/s/p5b-commit-failure"
        ),
        tg_id=75501,
    )
    events: list[DomainEvent] = []
    original_commit = db_session.commit
    commit_calls = 0

    async def fail_first_commit() -> None:
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise RuntimeError("simulated commit failure")
        await original_commit()

    monkeypatch.setattr(db_session, "commit", fail_first_commit)

    result = await RawMessageService(db_session).dedup_and_persist(
        message.id,
        event_bus=_recording_bus(events),
    )

    assert result.dedup_status == "dedup_pending"
    assert events == []
    assert await _count(db_session, Work) == 0
    assert await _count(db_session, Resource) == 0
    assert await _count(db_session, ResourceLink) == 0
    assert await _count(db_session, ResourceSource) == 0


@pytest.mark.asyncio
async def test_publish_failure_does_not_rollback_committed_data(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = await _ingest_and_parse(
        db_session,
        raw_text=(
            "【家业】第1集 夸克 "
            "https://pan.quark.cn/s/p5b-publish-failure"
        ),
        tg_id=75601,
    )

    class FailingEventBus(InMemoryEventBus):
        async def publish(self, event: DomainEvent) -> None:
            raise RuntimeError("simulated publish failure")

    result = await RawMessageService(db_session).dedup_and_persist(
        message.id,
        event_bus=FailingEventBus(),
    )

    assert result.dedup_status == "new"
    assert "simulated publish failure" in caplog.text

    async with AsyncSession(bind=db_session.bind) as observer:
        persisted_message = await observer.get(RawMessage, message.id)
        assert persisted_message is not None
        assert persisted_message.dedup_status == "new"
        assert await _count(observer, Work) == 1
        assert await _count(observer, Resource) == 1
        assert await _count(observer, ResourceLink) == 1
        assert await _count(observer, ResourceSource) == 1
