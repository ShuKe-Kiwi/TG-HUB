"""P4-C: RawMessage.parsed_data → DedupService integration tests.

Acceptance criteria (8 tests):
  1. parsed RawMessage → new Work/Resource/Link/Source, dedup_status=new
  2. Same resource_key, 2nd RawMessage → merge, dedup_status=matched
  3. parsed_data=[] → dedup_status=skipped
  4. parsed_data=None (no parse) → dedup_status=skipped
  5. Malformed parsed_data → rollback, dedup_status=dedup_pending
  6. DedupService exception → rollback, counts unchanged
  7. Multi-resource message → multiple ResourceSource, dedup_status=new
  8. Idempotent call → 1st=new, 2nd=matched, counts unchanged
"""

from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.parser.dto import LinkProvider, ParsedLink, ParsedResource
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService
from app.modules.resource.model import Resource, ResourceLink, ResourceSource, Work
from app.modules.resource.service import DedupService


# ========================================================================
# Helpers
# ========================================================================

async def _ingest_and_parse(
    db_session,
    raw_text: str,
    tg_id: int,
) -> RawMessage:
    """Create Channel → Ingest → Parse → return RawMessage with parsed_data."""
    channel = await ChannelService(db_session).create_or_get(
        ChannelCreate(name="P4-C 测试频道", tg_id=tg_id),
    )
    msg = await RawMessageService(db_session).ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=tg_id,
            raw_text=raw_text,
        ),
    )
    return await RawMessageService(db_session).parse_and_persist(msg.id)


async def _count_table(db_session, model) -> int:
    """Convenience: COUNT(*) for a given model."""
    result = await db_session.execute(select(func.count(model.id)))
    return result.scalar_one()


# ========================================================================
# Tests
# ========================================================================


class TestDedupAndPersist:

    # ----------------------------------------------------------
    # 1. New resource path
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_dedup_new_resource(self, db_session):
        msg = await _ingest_and_parse(
            db_session,
            "【家业】第1集 1080p 夸克 https://pan.quark.cn/s/p4c001 提取码 abc",
            tg_id=74101,
        )

        result = await RawMessageService(db_session).dedup_and_persist(msg.id)

        assert result.dedup_status == "new"
        assert await _count_table(db_session, Work) == 1
        assert await _count_table(db_session, Resource) == 1
        assert await _count_table(db_session, ResourceLink) == 1
        assert await _count_table(db_session, ResourceSource) == 1

        # Verify resource_type alignment
        resource = (await db_session.execute(
            select(Resource).where(Resource.resource_key.ilike("%家业%"))
        )).scalar_one()
        assert resource.resource_type == "single_episode"
        work = await db_session.get(Work, resource.work_id)
        assert work is not None and work.type == "drama"

    # ----------------------------------------------------------
    # 2. Merge path
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_dedup_merge_existing(self, db_session):
        msg1 = await _ingest_and_parse(
            db_session,
            "【家业】第1集 夸克 https://pan.quark.cn/s/p4c002a 提取码 xyz",
            tg_id=74201,
        )
        await RawMessageService(db_session).dedup_and_persist(msg1.id)

        msg2 = await _ingest_and_parse(
            db_session,
            "【家业】第1集 百度 https://pan.baidu.com/s/p4c002b 提取码 abc",
            tg_id=74202,
        )
        result2 = await RawMessageService(db_session).dedup_and_persist(msg2.id)

        assert result2.dedup_status == "matched"
        assert await _count_table(db_session, Work) == 1
        assert await _count_table(db_session, Resource) == 1
        # Links: two different providers → 2 links
        assert await _count_table(db_session, ResourceLink) == 2
        # Sources: two messages → 2 sources
        sources = (await db_session.execute(
            select(ResourceSource)
        )).scalars().all()
        assert len(sources) == 2

    # ----------------------------------------------------------
    # 3. Empty parsed_data
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_dedup_skip_empty_parsed_data(self, db_session):
        # A message with no links → parse returns None → parsed_data is None
        msg = await _ingest_and_parse(
            db_session,
            "纯文本没有链接",
            tg_id=74301,
        )
        # parse_and_persist will set parse_status=parse_failed, parsed_data=None
        assert msg.parsed_data is None

        result = await RawMessageService(db_session).dedup_and_persist(msg.id)

        assert result.dedup_status == "skipped"
        assert await _count_table(db_session, Work) == 0
        assert await _count_table(db_session, Resource) == 0

    # ----------------------------------------------------------
    # 4. None parsed_data (ingest only, no parse)
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_dedup_skip_none_parsed_data(self, db_session):
        channel = await ChannelService(db_session).create_or_get(
            ChannelCreate(name="P4-C 无解析", tg_id=74401),
        )
        msg = await RawMessageService(db_session).ingest(
            RawMessageCreate(
                channel_id=channel.id,
                tg_message_id=74401,
                raw_text="【家业】第1集 夸克 https://pan.quark.cn/s/p4c004",
            ),
        )
        assert msg.parsed_data is None

        result = await RawMessageService(db_session).dedup_and_persist(msg.id)

        assert result.dedup_status == "skipped"
        assert await _count_table(db_session, Work) == 0
        assert await _count_table(db_session, Resource) == 0

    # ----------------------------------------------------------
    # 5. Malformed parsed_data → rollback → dedup_pending
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_deserialize_invalid_field(self, db_session):
        msg = await _ingest_and_parse(
            db_session,
            "【家业】第1集 夸克 https://pan.quark.cn/s/p4c005",
            tg_id=74501,
        )
        # Corrupt parsed_data with invalid provider type
        msg.parsed_data = [
            {
                "title": "家业",
                "raw_title": "家业 EP1",
                "links": [
                    {"provider": 12345, "original_text": "bad"},
                ],
            },
        ]
        await db_session.commit()

        # Capture counts before
        work_before = await _count_table(db_session, Work)
        res_before = await _count_table(db_session, Resource)

        result = await RawMessageService(db_session).dedup_and_persist(msg.id)

        assert result.dedup_status == "dedup_pending"
        assert await _count_table(db_session, Work) == work_before
        assert await _count_table(db_session, Resource) == res_before

    # ----------------------------------------------------------
    # 6. DedupService exception → rollback → dedup_pending, no dirty data
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_dedup_exception_rollback(self, db_session):
        msg = await _ingest_and_parse(
            db_session,
            "【家业】第1集 夸克 https://pan.quark.cn/s/p4c006",
            tg_id=74601,
        )

        class FailingDedup(DedupService):
            async def dedup(self, **kwargs):
                raise RuntimeError("dedup engine failure")

        # Capture counts before
        work_before = await _count_table(db_session, Work)
        res_before = await _count_table(db_session, Resource)
        link_before = await _count_table(db_session, ResourceLink)
        src_before = await _count_table(db_session, ResourceSource)

        result = await RawMessageService(
            db_session,
            dedup_service=FailingDedup(db_session),
        ).dedup_and_persist(msg.id)

        assert result.dedup_status == "dedup_pending"
        # All counts unchanged — no dirty data
        assert await _count_table(db_session, Work) == work_before
        assert await _count_table(db_session, Resource) == res_before
        assert await _count_table(db_session, ResourceLink) == link_before
        assert await _count_table(db_session, ResourceSource) == src_before

    # ----------------------------------------------------------
    # 7. Multi-resource message (manually constructed)
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_dedup_multi_resource(self, db_session):
        """A single RawMessage whose parsed_data contains TWO ParsedResource dicts."""
        channel = await ChannelService(db_session).create_or_get(
            ChannelCreate(name="P4-C 多资源", tg_id=74701),
        )
        msg = await RawMessageService(db_session).ingest(
            RawMessageCreate(
                channel_id=channel.id,
                tg_message_id=74701,
                raw_text="多资源测试",
            ),
        )
        # Manually set parsed_data to 2 resources
        msg.parsed_data = [
            {
                "title": "家业",
                "raw_title": "家业 第1集",
                "resource_type": "drama",
                "links": [
                    {
                        "provider": "quark",
                        "original_text": "夸克 https://pan.quark.cn/s/p4c007a",
                        "url": "https://pan.quark.cn/s/p4c007a",
                        "link_type": "url",
                    },
                ],
                "metadata": {"episode_no": 1, "episode_range": "ep1"},
                "tags": [],
                "confidence": 1.0,
                "parser_version": "0.2.0",
                "rule_version": "0.2.0",
            },
            {
                "title": "家业",
                "raw_title": "家业 第2集",
                "resource_type": "drama",
                "links": [
                    {
                        "provider": "baidu",
                        "original_text": "百度 https://pan.baidu.com/s/p4c007b",
                        "url": "https://pan.baidu.com/s/p4c007b",
                        "link_type": "url",
                    },
                ],
                "metadata": {"episode_no": 2, "episode_range": "ep2"},
                "tags": [],
                "confidence": 1.0,
                "parser_version": "0.2.0",
                "rule_version": "0.2.0",
            },
        ]
        await db_session.commit()

        result = await RawMessageService(db_session).dedup_and_persist(msg.id)

        assert result.dedup_status == "new"
        assert await _count_table(db_session, Resource) == 2
        assert await _count_table(db_session, ResourceSource) == 2

    # ----------------------------------------------------------
    # 8. Idempotent — same message deduped twice
    # ----------------------------------------------------------
    @pytest.mark.asyncio
    async def test_dedup_idempotent(self, db_session):
        msg = await _ingest_and_parse(
            db_session,
            "【家业】第1集 夸克 https://pan.quark.cn/s/p4c008 提取码 123",
            tg_id=74801,
        )

        # First call: creates new resource
        r1 = await RawMessageService(db_session).dedup_and_persist(msg.id)
        assert r1.dedup_status == "new"

        src_after_first = await _count_table(db_session, ResourceSource)
        link_after_first = await _count_table(db_session, ResourceLink)

        # Second call: merge path — ResourceSource already exists
        r2 = await RawMessageService(db_session).dedup_and_persist(msg.id)
        assert r2.dedup_status == "matched"

        # Counts unchanged
        assert await _count_table(db_session, ResourceSource) == src_after_first
        assert await _count_table(db_session, ResourceLink) == link_after_first
        assert await _count_table(db_session, Work) == 1
        assert await _count_table(db_session, Resource) == 1

        # source_count verified
        resource = (await db_session.execute(
            select(Resource).where(Resource.resource_key.ilike("%家业%"))
        )).scalar_one()
        assert resource.source_count == src_after_first
