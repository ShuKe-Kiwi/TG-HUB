"""P4-D: MVP-A End-to-End acceptance tests.

Tests the full ingest → parse → dedup pipeline end-to-end.
No business logic changes — pure acceptance verification.

Boundary:
- Only existing Parser fixtures (drama only, no parser extension)
- No EventBus / Bot / Transfer / scheduled tasks / Web UI / AI
- No modifications to ARCHITECTURE.md, model.py, dto.py, service.py, repository.py
"""

import pytest
from sqlalchemy import func, select

from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService
from app.modules.resource.model import Resource, ResourceLink, ResourceSource, Work


# ========================================================================
# Helpers
# ========================================================================

async def _ingest_parse_dedup(db_session, raw_text: str, tg_id: int) -> RawMessage:
    """Full pipeline: Channel → Ingest → Parse → Dedup → return RawMessage."""
    channel = await ChannelService(db_session).create_or_get(
        ChannelCreate(name="P4-D E2E", tg_id=tg_id),
    )
    msg = await RawMessageService(db_session).ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=tg_id,
            raw_text=raw_text,
        ),
    )
    parsed = await RawMessageService(db_session).parse_and_persist(msg.id)
    return await RawMessageService(db_session).dedup_and_persist(parsed.id)


async def _count(db_session, model) -> int:
    result = await db_session.execute(select(func.count(model.id)))
    return result.scalar_one()


# ========================================================================
# Tests
# ========================================================================


class TestE2eCreatePath:
    """Acceptance 1: a single parsed message creates Work/Resource/Link/Source."""

    @pytest.mark.asyncio
    async def test_e2e_create_path(self, db_session):
        msg = await _ingest_parse_dedup(
            db_session,
            "【家业】第1集 1080p 夸克 https://pan.quark.cn/s/p4d001 提取码 abc",
            tg_id=74001,
        )

        assert msg.dedup_status == "new"
        assert await _count(db_session, Work) == 1
        assert await _count(db_session, Resource) == 1
        assert await _count(db_session, ResourceLink) == 1
        assert await _count(db_session, ResourceSource) == 1

        # Resource type alignment
        resource = (await db_session.execute(
            select(Resource).where(Resource.resource_key.ilike("%家业%"))
        )).scalar_one()
        assert resource.resource_type == "single_episode"

        work = await db_session.get(Work, resource.work_id)
        assert work is not None
        assert work.type == "drama"


class TestE2eMergePath:
    """Acceptance 2: same resource_key, different provider → merge."""

    @pytest.mark.asyncio
    async def test_e2e_merge_path(self, db_session):
        # First message: create path
        await _ingest_parse_dedup(
            db_session,
            "【家业】第1集 夸克 https://pan.quark.cn/s/p4d002a 提取码 abc",
            tg_id=74002,
        )

        # Second message: same resource_key, different provider (baidu)
        msg2 = await _ingest_parse_dedup(
            db_session,
            "【家业】第1集 百度 https://pan.baidu.com/s/p4d002b 提取码 def",
            tg_id=74003,
        )

        assert msg2.dedup_status == "matched"
        # Only one Resource created
        assert await _count(db_session, Resource) == 1
        # Two sources (two messages)
        assert await _count(db_session, ResourceSource) == 2
        # Two links (different providers)
        assert await _count(db_session, ResourceLink) == 2

        # source_count recalculated from actual count
        resource = (await db_session.execute(
            select(Resource).where(Resource.resource_key.ilike("%家业%"))
        )).scalar_one()
        assert resource.source_count == 2


class TestE2eSkipNoLinks:
    """Acceptance 3: text with no links → parse_failed → dedup skipped."""

    @pytest.mark.asyncio
    async def test_e2e_skip_no_links(self, db_session):
        channel = await ChannelService(db_session).create_or_get(
            ChannelCreate(name="P4-D 无链接", tg_id=74004),
        )
        msg = await RawMessageService(db_session).ingest(
            RawMessageCreate(
                channel_id=channel.id,
                tg_message_id=74004,
                raw_text="纯文本消息没有任何链接",
            ),
        )
        parsed = await RawMessageService(db_session).parse_and_persist(msg.id)

        assert parsed.parse_status == "parse_failed"

        result = await RawMessageService(db_session).dedup_and_persist(parsed.id)

        assert result.dedup_status == "skipped"
        # No phantom resources
        assert await _count(db_session, Work) == 0
        assert await _count(db_session, Resource) == 0
        assert await _count(db_session, ResourceLink) == 0
        assert await _count(db_session, ResourceSource) == 0


class TestE2eSecondResource:
    """Acceptance 4: a different drama → create path, foreign keys joinable."""

    @pytest.mark.asyncio
    async def test_e2e_second_resource(self, db_session):
        # Create first resource
        await _ingest_parse_dedup(
            db_session,
            "【家业】第1集 夸克 https://pan.quark.cn/s/p4d005",
            tg_id=74005,
        )

        # Second resource: different drama title → different resource_key
        msg2 = await _ingest_parse_dedup(
            db_session,
            "【唐朝诡事录】第1集 百度 https://pan.baidu.com/s/p4d006",
            tg_id=74006,
        )

        assert msg2.dedup_status == "new"
        # Two works, two resources
        assert await _count(db_session, Work) == 2
        assert await _count(db_session, Resource) == 2

        # Each resource belongs to its own work (foreign-key joinable)
        resources = (await db_session.execute(
            select(Resource).order_by(Resource.id)
        )).scalars().all()
        assert len(resources) == 2

        for res in resources:
            work = await db_session.get(Work, res.work_id)
            assert work is not None
            assert work.id == res.work_id

        # ResourceLink and ResourceSource exist for both
        assert await _count(db_session, ResourceLink) == 2
        assert await _count(db_session, ResourceSource) == 2
