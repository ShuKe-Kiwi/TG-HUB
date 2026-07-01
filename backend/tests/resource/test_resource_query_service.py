"""P5-C PostgreSQL tests for read-only Resource query projections."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channel.model import Channel
from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService
from app.modules.resource.model import (
    Resource,
    ResourceLink,
    ResourceSource,
    Work,
)
from app.modules.resource.query_schema import (
    LinkView,
    ResourceDetail,
    ResourceListItem,
    SourceView,
)
from app.modules.resource.query_service import ResourceQueryService

_BASE_TIME = datetime(2026, 7, 2, 8, 0, tzinfo=timezone.utc)


async def _seed_resource(
    db_session: AsyncSession,
    *,
    key: str,
    resource_title: str,
    work_title: str,
    last_seen_at: datetime,
    work_status: str = "active",
    resource_status: str = "active",
) -> tuple[Work, Resource]:
    work = Work(
        title=work_title,
        title_norm=work_title.lower(),
        type="drama",
        aliases=[],
        year=2026,
        work_key=f"drama:{key}",
        status=work_status,
    )
    db_session.add(work)
    await db_session.flush()

    resource = Resource(
        work_id=work.id,
        title=resource_title,
        title_norm=resource_title.lower(),
        resource_type="single_episode",
        episode_no=1,
        season_no=1,
        episode_range="ep1",
        year=2026,
        quality="1080p",
        resource_key=f"drama:{key}:ep1",
        episode_key=f"drama:{key}:s01e1",
        content_fingerprint=key.encode().hex().ljust(64, "0")[:64],
        description=f"{resource_title} description",
        tags=["drama"],
        status=resource_status,
        source_count=0,
        first_seen_at=last_seen_at,
        last_seen_at=last_seen_at,
    )
    db_session.add(resource)
    await db_session.flush()
    return work, resource


async def _ingest_parse_dedup(
    db_session: AsyncSession,
    *,
    raw_text: str,
    tg_id: int,
) -> RawMessage:
    channel = await ChannelService(db_session).create_or_get(
        ChannelCreate(name=f"P5-C merge {tg_id}", tg_id=tg_id),
    )
    message = await RawMessageService(db_session).ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=tg_id,
            raw_text=raw_text,
        )
    )
    parsed = await RawMessageService(db_session).parse_and_persist(message.id)
    return await RawMessageService(db_session).dedup_and_persist(parsed.id)


async def _registry_counts(bind: object) -> tuple[int, int, int, int]:
    async with AsyncSession(bind=bind) as observer:
        counts: list[int] = []
        for model in (Work, Resource, ResourceLink, ResourceSource):
            result = await observer.execute(select(func.count(model.id)))
            counts.append(result.scalar_one())
        return counts[0], counts[1], counts[2], counts[3]


@pytest.mark.asyncio
async def test_latest_has_stable_order_limit_and_offset(
    db_session: AsyncSession,
) -> None:
    _, first = await _seed_resource(
        db_session,
        key="latest-first",
        resource_title="First",
        work_title="First Work",
        last_seen_at=_BASE_TIME,
    )
    _, second = await _seed_resource(
        db_session,
        key="latest-second",
        resource_title="Second",
        work_title="Second Work",
        last_seen_at=_BASE_TIME + timedelta(hours=1),
    )
    _, third = await _seed_resource(
        db_session,
        key="latest-third",
        resource_title="Third",
        work_title="Third Work",
        last_seen_at=_BASE_TIME + timedelta(hours=1),
    )
    await db_session.commit()

    service = ResourceQueryService(db_session)
    first_page = await service.latest(limit=2)
    second_page = await service.latest(limit=2, offset=1)

    assert [item.resource_id for item in first_page] == [third.id, second.id]
    assert [item.resource_id for item in second_page] == [second.id, first.id]
    assert all(isinstance(item, ResourceListItem) for item in first_page)
    assert all(not hasattr(item, "_sa_instance_state") for item in first_page)


@pytest.mark.asyncio
async def test_merge_updates_latest_order(db_session: AsyncSession) -> None:
    first = await _ingest_parse_dedup(
        db_session,
        raw_text=(
            "【家业】第1集 夸克 "
            "https://pan.quark.cn/s/p5c-order-a"
        ),
        tg_id=76101,
    )
    assert first.dedup_status == "new"
    second = await _ingest_parse_dedup(
        db_session,
        raw_text=(
            "【唐朝诡事录】第1集 夸克 "
            "https://pan.quark.cn/s/p5c-order-b"
        ),
        tg_id=76102,
    )
    assert second.dedup_status == "new"

    service = ResourceQueryService(db_session)
    before_merge = await service.latest()
    assert before_merge[0].title == "唐朝诡事录"

    merged = await _ingest_parse_dedup(
        db_session,
        raw_text=(
            "【家业】第1集 百度 "
            "https://pan.baidu.com/s/p5c-order-c"
        ),
        tg_id=76103,
    )
    assert merged.dedup_status == "matched"

    after_merge = await service.latest()
    assert after_merge[0].title == "家业"


@pytest.mark.asyncio
async def test_search_matches_resource_title(db_session: AsyncSession) -> None:
    _, resource = await _seed_resource(
        db_session,
        key="resource-title",
        resource_title="银河档案 EP1",
        work_title="Different Work",
        last_seen_at=_BASE_TIME,
    )
    await db_session.commit()

    results = await ResourceQueryService(db_session).search("银河档案")

    assert [item.resource_id for item in results] == [resource.id]


@pytest.mark.asyncio
async def test_search_matches_work_title(db_session: AsyncSession) -> None:
    _, resource = await _seed_resource(
        db_session,
        key="work-title",
        resource_title="Episode One",
        work_title="星河作品",
        last_seen_at=_BASE_TIME,
    )
    await db_session.commit()

    results = await ResourceQueryService(db_session).search("星河作品")

    assert [item.resource_id for item in results] == [resource.id]


@pytest.mark.parametrize(
    ("query", "target_title"),
    [
        ("%", "百分百%资源"),
        ("_", "下划线_资源"),
        ("\\", "路径\\资源"),
    ],
)
@pytest.mark.asyncio
async def test_search_treats_like_metacharacters_as_literals(
    db_session: AsyncSession,
    query: str,
    target_title: str,
) -> None:
    _, target = await _seed_resource(
        db_session,
        key=f"literal-{ord(query)}",
        resource_title=target_title,
        work_title="Literal Work",
        last_seen_at=_BASE_TIME,
    )
    await _seed_resource(
        db_session,
        key=f"ordinary-{ord(query)}",
        resource_title="普通资源",
        work_title="Ordinary Work",
        last_seen_at=_BASE_TIME + timedelta(minutes=1),
    )
    await db_session.commit()

    results = await ResourceQueryService(db_session).search(query)

    assert [item.resource_id for item in results] == [target.id]


@pytest.mark.parametrize("query", ["", "   "])
@pytest.mark.asyncio
async def test_search_empty_query_returns_empty_list(
    db_session: AsyncSession,
    query: str,
) -> None:
    assert await ResourceQueryService(db_session).search(query) == []


@pytest.mark.asyncio
async def test_detail_builds_nested_scalar_views_in_fixed_query_count(
    db_session: AsyncSession,
) -> None:
    _, resource = await _seed_resource(
        db_session,
        key="detail",
        resource_title="Detail Resource",
        work_title="Detail Work",
        last_seen_at=_BASE_TIME,
    )
    resource.source_count = 2

    first_channel = Channel(
        name="First Channel",
        tg_id=76201,
        tg_username="first_channel",
    )
    second_channel = Channel(
        name="Second Channel",
        tg_id=76202,
        tg_username=None,
    )
    db_session.add_all([first_channel, second_channel])
    await db_session.flush()

    first_message = RawMessage(
        channel_id=first_channel.id,
        tg_message_id=76201,
        raw_text="first",
    )
    second_message = RawMessage(
        channel_id=second_channel.id,
        tg_message_id=76202,
        raw_text="second",
    )
    db_session.add_all([first_message, second_message])
    await db_session.flush()

    db_session.add_all(
        [
            ResourceLink(
                resource_id=resource.id,
                provider="quark",
                original_text="quark link",
                original_url="https://quark.example/original",
                normalized_url="https://quark.example/normalized",
                url_hash="q" * 64,
                access_code="Q123",
                password=None,
                link_type="url",
                status="active",
            ),
            ResourceLink(
                resource_id=resource.id,
                provider="baidu",
                original_text="baidu link",
                original_url="https://baidu.example/share",
                normalized_url=None,
                url_hash="b" * 64,
                access_code=None,
                password="B456",
                link_type="url",
                status="unknown",
            ),
            ResourceSource(
                resource_id=resource.id,
                raw_message_id=first_message.id,
                channel_id=first_channel.id,
                match_type="title_episode",
                confidence=0.9,
                matched_reason="first source",
                parsed_snapshot={"secret": "must not leak"},
                parser_version="0.2.0",
                rule_version="0.2.0",
                detected_at=_BASE_TIME,
            ),
            ResourceSource(
                resource_id=resource.id,
                raw_message_id=second_message.id,
                channel_id=second_channel.id,
                match_type="title_episode",
                confidence=0.95,
                matched_reason="second source",
                parsed_snapshot={"secret": "must not leak either"},
                parser_version="0.2.0",
                rule_version="0.2.0",
                detected_at=_BASE_TIME + timedelta(hours=1),
            ),
        ]
    )
    await db_session.commit()

    statements: list[str] = []
    bind = db_session.bind
    assert bind is not None

    def capture_select(
        conn,
        cursor,
        statement: str,
        parameters,
        context,
        executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(bind.sync_engine, "before_cursor_execute", capture_select)
    try:
        detail = await ResourceQueryService(db_session).get_detail(resource.id)
    finally:
        event.remove(bind.sync_engine, "before_cursor_execute", capture_select)

    assert isinstance(detail, ResourceDetail)
    assert detail.work_title == "Detail Work"
    assert detail.content_type == "drama"
    assert [link.provider for link in detail.links] == ["baidu", "quark"]
    assert detail.links[0].url == "https://baidu.example/share"
    assert detail.links[1].url == "https://quark.example/normalized"
    assert all(isinstance(link, LinkView) for link in detail.links)

    assert [source.matched_reason for source in detail.sources] == [
        "second source",
        "first source",
    ]
    assert detail.sources[0].channel_title == "Second Channel"
    assert detail.sources[0].channel_username is None
    assert detail.sources[0].source_id > 0
    assert detail.sources[0].raw_message_id == second_message.id
    assert detail.sources[0].channel_id == second_channel.id
    assert detail.sources[0].match_type == "title_episode"
    assert detail.sources[0].confidence == 0.95
    assert detail.sources[0].parser_version == "0.2.0"
    assert detail.sources[0].rule_version == "0.2.0"
    assert detail.sources[0].detected_at == _BASE_TIME + timedelta(hours=1)
    assert detail.sources[1].channel_title == "First Channel"
    assert detail.sources[1].channel_username == "first_channel"
    assert all(isinstance(source, SourceView) for source in detail.sources)
    assert len(statements) == 3

    assert not hasattr(detail, "_sa_instance_state")
    assert all(not hasattr(link, "_sa_instance_state") for link in detail.links)
    assert all(
        not hasattr(source, "_sa_instance_state") for source in detail.sources
    )
    dumped_sources = detail.model_dump()["sources"]
    assert all("parsed_snapshot" not in source for source in dumped_sources)


@pytest.mark.asyncio
async def test_get_detail_missing_resource_returns_none(
    db_session: AsyncSession,
) -> None:
    assert await ResourceQueryService(db_session).get_detail(999999) is None


@pytest.mark.asyncio
async def test_inactive_work_or_resource_is_hidden(
    db_session: AsyncSession,
) -> None:
    _, active = await _seed_resource(
        db_session,
        key="active",
        resource_title="Visible",
        work_title="Visible Work",
        last_seen_at=_BASE_TIME,
    )
    _, inactive_work_resource = await _seed_resource(
        db_session,
        key="inactive-work",
        resource_title="Hidden Work Resource",
        work_title="Hidden Work",
        last_seen_at=_BASE_TIME + timedelta(hours=1),
        work_status="inactive",
    )
    _, inactive_resource = await _seed_resource(
        db_session,
        key="inactive-resource",
        resource_title="Hidden Resource",
        work_title="Active Parent Work",
        last_seen_at=_BASE_TIME + timedelta(hours=2),
        resource_status="inactive",
    )
    await db_session.commit()

    service = ResourceQueryService(db_session)

    assert [item.resource_id for item in await service.latest()] == [active.id]
    assert await service.search("Hidden") == []
    assert await service.get_detail(inactive_work_resource.id) is None
    assert await service.get_detail(inactive_resource.id) is None


@pytest.mark.asyncio
async def test_queries_do_not_flush_commit_or_change_registry_state(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, resource = await _seed_resource(
        db_session,
        key="read-only",
        resource_title="Read Only",
        work_title="Read Only Work",
        last_seen_at=_BASE_TIME,
    )
    await db_session.commit()
    bind = db_session.bind
    assert bind is not None
    before_counts = await _registry_counts(bind)

    async def forbidden_write(*args, **kwargs) -> None:
        raise AssertionError("query service attempted a write operation")

    monkeypatch.setattr(db_session, "flush", forbidden_write)
    monkeypatch.setattr(db_session, "commit", forbidden_write)

    service = ResourceQueryService(db_session)
    await service.latest()
    await service.search("Read")
    await service.get_detail(resource.id)

    after_counts = await _registry_counts(bind)
    assert after_counts == before_counts
    assert not db_session.new
    assert not db_session.dirty
    assert not db_session.deleted


@pytest.mark.asyncio
async def test_pagination_validation(db_session: AsyncSession) -> None:
    service = ResourceQueryService(db_session)

    for invalid_limit in (0, 51):
        with pytest.raises(ValueError, match="limit"):
            await service.latest(limit=invalid_limit)

    with pytest.raises(ValueError, match="offset"):
        await service.search("query", offset=-1)
