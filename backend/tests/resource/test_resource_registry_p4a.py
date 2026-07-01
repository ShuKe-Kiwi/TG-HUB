"""P4-A PostgreSQL tests for Resource Registry models and repositories."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.modules.channel.model import Channel
from app.modules.channel.repository import ChannelRepository
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.repository import RawMessageRepository
from app.modules.resource.model import (
    Resource,
    ResourceLink,
    ResourceSource,
    Work,
)
from app.modules.resource.repository import (
    ResourceLinkRepository,
    ResourceRepository,
    ResourceSourceRepository,
    WorkRepository,
)


async def _create_work(
    db_session,
    *,
    work_key: str = "drama:家业:2026",
) -> Work:
    return await WorkRepository(db_session).create(
        Work(
            title="家业",
            title_norm="家业",
            type="drama",
            aliases=[],
            year=2026,
            work_key=work_key,
        )
    )


async def _create_resource(
    db_session,
    work: Work,
    *,
    resource_key: str = "drama:家业:ep1",
) -> Resource:
    return await ResourceRepository(db_session).create(
        Resource(
            work_id=work.id,
            title="家业 第1集",
            title_norm="家业",
            resource_type="single_episode",
            episode_no=1,
            season_no=1,
            episode_range="ep1",
            year=2026,
            resource_key=resource_key,
            episode_key="drama:家业:s01:e01",
            content_fingerprint="a" * 64,
            tags=[],
        )
    )


async def _create_channel_and_raw_message(db_session):
    channel = await ChannelRepository(db_session).create(
        Channel(
            name="P4-A 测试频道",
            tg_id=94001,
            source_type="telegram",
            status="active",
        )
    )
    raw_message = await RawMessageRepository(db_session).create(
        RawMessage(
            channel_id=channel.id,
            tg_message_id=94001,
            raw_text="家业 第一集",
            ingest_status="stored",
            parse_status="parsed",
            dedup_status="dedup_pending",
            parse_attempts=1,
        )
    )
    return channel, raw_message


async def _assert_unique_violation(db_session, operation, constraint_name):
    with pytest.raises(IntegrityError) as exc_info:
        await operation
    assert constraint_name in str(exc_info.value.orig)
    await db_session.rollback()


@pytest.mark.asyncio
async def test_resource_registry_uses_postgresql_constraints(db_session):
    assert db_session.get_bind().dialect.name == "postgresql"

    result = await db_session.execute(
        text(
            """
            SELECT conname
            FROM pg_constraint
            WHERE conname IN (
                'uq_works_work_key',
                'uq_resources_resource_key',
                'uq_resource_links_resource_provider_urlhash',
                'uq_resource_sources_resource_rawmsg'
            )
            """
        )
    )
    assert set(result.scalars()) == {
        "uq_works_work_key",
        "uq_resources_resource_key",
        "uq_resource_links_resource_provider_urlhash",
        "uq_resource_sources_resource_rawmsg",
    }


@pytest.mark.asyncio
async def test_minimal_repositories_create_and_get_registry_records(db_session):
    work = await _create_work(db_session)
    resource = await _create_resource(db_session, work)
    link = await ResourceLinkRepository(db_session).create(
        ResourceLink(
            resource_id=resource.id,
            provider="quark",
            original_text="https://pan.quark.cn/s/p4a",
            original_url="https://pan.quark.cn/s/p4a",
            normalized_url="https://pan.quark.cn/s/p4a",
            url_hash="b" * 64,
            share_id="p4a",
        )
    )
    channel, raw_message = await _create_channel_and_raw_message(db_session)
    source = await ResourceSourceRepository(db_session).create(
        ResourceSource(
            resource_id=resource.id,
            raw_message_id=raw_message.id,
            channel_id=channel.id,
            match_type="title_episode",
            confidence=1.0,
            matched_reason="P4-A repository smoke test",
            parsed_snapshot=[{"title": "家业"}],
            parser_version="0.2.0",
            rule_version="0.2.0",
        )
    )

    assert (
        await WorkRepository(db_session).get_by_work_key(work.work_key)
    ).id == work.id
    assert (
        await ResourceRepository(db_session).get_by_resource_key(
            resource.resource_key
        )
    ).id == resource.id
    assert (
        await ResourceLinkRepository(db_session).get_by_identity(
            resource.id,
            link.provider,
            link.url_hash,
        )
    ).id == link.id
    assert (
        await ResourceSourceRepository(db_session).get_by_identity(
            resource.id,
            raw_message.id,
        )
    ).id == source.id


@pytest.mark.asyncio
async def test_work_key_unique_constraint_is_enforced_by_postgresql(db_session):
    await _create_work(db_session)

    duplicate = WorkRepository(db_session).create(
        Work(
            title="另一个标题",
            title_norm="另一个标题",
            type="drama",
            aliases=[],
            year=2026,
            work_key="drama:家业:2026",
        )
    )
    await _assert_unique_violation(
        db_session,
        duplicate,
        "uq_works_work_key",
    )


@pytest.mark.asyncio
async def test_resource_key_unique_constraint_is_enforced_by_postgresql(
    db_session,
):
    work = await _create_work(db_session)
    await _create_resource(db_session, work)

    duplicate = ResourceRepository(db_session).create(
        Resource(
            work_id=work.id,
            title="重复资源",
            title_norm="重复资源",
            resource_type="single_episode",
            episode_no=99,
            season_no=1,
            episode_range="ep99",
            resource_key="drama:家业:ep1",
            episode_key="drama:重复资源:s01:e99",
            content_fingerprint="c" * 64,
            tags=[],
        )
    )
    await _assert_unique_violation(
        db_session,
        duplicate,
        "uq_resources_resource_key",
    )


@pytest.mark.asyncio
async def test_resource_link_tuple_unique_constraint_is_enforced_by_postgresql(
    db_session,
):
    work = await _create_work(db_session)
    resource = await _create_resource(db_session, work)
    repository = ResourceLinkRepository(db_session)
    await repository.create(
        ResourceLink(
            resource_id=resource.id,
            provider="quark",
            original_text="first",
            url_hash="d" * 64,
        )
    )

    duplicate = repository.create(
        ResourceLink(
            resource_id=resource.id,
            provider="quark",
            original_text="second",
            url_hash="d" * 64,
        )
    )
    await _assert_unique_violation(
        db_session,
        duplicate,
        "uq_resource_links_resource_provider_urlhash",
    )


@pytest.mark.asyncio
async def test_resource_source_tuple_unique_constraint_is_enforced_by_postgresql(
    db_session,
):
    work = await _create_work(db_session)
    resource = await _create_resource(db_session, work)
    channel, raw_message = await _create_channel_and_raw_message(db_session)
    repository = ResourceSourceRepository(db_session)
    await repository.create(
        ResourceSource(
            resource_id=resource.id,
            raw_message_id=raw_message.id,
            channel_id=channel.id,
            match_type="title_episode",
            confidence=1.0,
            matched_reason="first",
            parsed_snapshot=[],
            parser_version="0.2.0",
            rule_version="0.2.0",
        )
    )

    duplicate = repository.create(
        ResourceSource(
            resource_id=resource.id,
            raw_message_id=raw_message.id,
            channel_id=channel.id,
            match_type="manual",
            confidence=0.5,
            matched_reason="second",
            parsed_snapshot=[],
            parser_version="0.2.0",
            rule_version="0.2.0",
        )
    )
    await _assert_unique_violation(
        db_session,
        duplicate,
        "uq_resource_sources_resource_rawmsg",
    )
