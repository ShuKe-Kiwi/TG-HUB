"""P4-B: DedupService full test suite.

Acceptance criteria:
  1. resource_key hit → merge into existing Resource
  2. resource_key miss → create Work + Resource
  3. Same ResourceLink not inserted twice
  4. ResourceSource tracks raw_message_id / channel_id
  5. source_count recalculated from actual COUNT
  6. matched_reason human-readable
  7. Idempotent under PostgreSQL unique constraints
  8. Resource.resource_type = episode_kind; Work.type = content_type
"""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.channel.model import Channel
from app.modules.normalizer.core import (
    NormalizedResource,
    normalize_resource,
)
from app.modules.parser.dto import (
    LinkProvider,
    ParsedLink,
    ParsedMetadata,
    ParsedResource,
)
from app.modules.rawmessage.model import RawMessage
from app.modules.resource.model import (
    Resource,
    ResourceLink,
    ResourceSource,
    Work,
)
from app.modules.resource.repository import WorkRepository
from app.modules.resource.schema import DedupResult
from app.modules.resource.service import (
    DedupService,
    deserialize_parsed_resource,
    serialize_parsed_resource,
)


# ========================================================================
# Test data helpers
# ========================================================================

def _make_parsed_resource(
    title: str = "家业",
    episode_no: int | None = 1,
    episode_range: str | None = "ep1",
    parsed_metadata: ParsedMetadata | None = None,
    links: list[ParsedLink] | None = None,
    resource_type: str = "drama",
) -> ParsedResource:
    metadata = parsed_metadata
    if metadata is None and episode_no is not None:
        metadata = ParsedMetadata(
            episode_no=episode_no,
            episode_range=episode_range,
        )
    if links is None:
        links = [
            ParsedLink(
                provider=LinkProvider.QUARK,
                original_text="夸克网盘 https://pan.quark.cn/s/abc123 提取码 xyz789",
                url="https://pan.quark.cn/s/abc123",
                share_id="abc123",
                access_code="xyz789",
                link_type="url",
            ),
        ]
    return ParsedResource(
        title=title,
        raw_title=f"{title} 第{episode_no}集" if episode_no else title,
        resource_type=resource_type,
        links=links,
        metadata=metadata,
        tags=["HDR"],
        confidence=1.0,
        parser_version="0.2.0",
        rule_version="0.2.0",
    )


def _make_normalized(parsed: ParsedResource) -> NormalizedResource:
    return normalize_resource(parsed)


async def _seed_message(
    session: AsyncSession,
    raw_message_id: int,
    channel_id: int,
) -> None:
    """Create a Channel + RawMessage row if they don't already exist."""
    ch = await session.execute(
        select(Channel).where(Channel.id == channel_id)
    )
    if ch.scalar_one_or_none() is None:
        session.add(Channel(
            id=channel_id,
            name=f"ch{channel_id}",
            tg_id=channel_id,
        ))
        await session.flush()

    rm = await session.execute(
        select(RawMessage).where(RawMessage.id == raw_message_id)
    )
    if rm.scalar_one_or_none() is None:
        session.add(RawMessage(
            id=raw_message_id,
            channel_id=channel_id,
            tg_message_id=raw_message_id,
            raw_text="test",
        ))
        await session.flush()


# ========================================================================
# Serialization unit tests
# ========================================================================

class TestSerialize:
    """serialize_parsed_resource correctness."""

    def test_serialize_basic(self):
        parsed = _make_parsed_resource()
        result = serialize_parsed_resource(parsed)
        assert result["title"] == "家业"
        assert result["resource_type"] == "drama"
        assert result["confidence"] == 1.0
        assert result["parser_version"] == "0.2.0"
        assert result["links"][0]["provider"] == "quark"
        assert result["metadata"]["episode_no"] == 1

    def test_serialize_json_safe(self):
        import json
        parsed = _make_parsed_resource()
        json.dumps(serialize_parsed_resource(parsed))

    def test_serialize_none_metadata(self):
        parsed = _make_parsed_resource(parsed_metadata=None, episode_no=None)
        assert serialize_parsed_resource(parsed)["metadata"] is None

    def test_serialize_empty_links(self):
        parsed = _make_parsed_resource(links=[])
        assert serialize_parsed_resource(parsed)["links"] == []

    # ------------------------------------------------------------------
    # Deserialize (reverse symmetry)
    # ------------------------------------------------------------------

    def test_deserialize_roundtrip(self):
        """serialize → deserialize → field fidelity."""
        parsed = _make_parsed_resource()
        serialized = serialize_parsed_resource(parsed)
        restored = deserialize_parsed_resource(serialized)
        assert restored.title == parsed.title
        assert restored.raw_title == parsed.raw_title
        assert restored.resource_type == parsed.resource_type
        assert restored.confidence == parsed.confidence
        assert restored.parser_version == parsed.parser_version
        assert restored.links[0].provider == parsed.links[0].provider
        assert restored.metadata is not None
        assert restored.metadata.episode_no == parsed.metadata.episode_no

    def test_deserialize_missing_title(self):
        """Missing title/raw_title → ValueError."""
        with pytest.raises(ValueError, match="Missing required fields"):
            deserialize_parsed_resource({"links": []})

    def test_deserialize_provider_str(self):
        """provider='quark' (str) → LinkProvider.QUARK."""
        data = {
            "title": "T",
            "raw_title": "T",
            "links": [
                {"provider": "quark", "original_text": "t"},
            ],
        }
        restored = deserialize_parsed_resource(data)
        assert restored.links[0].provider == LinkProvider.QUARK

    def test_deserialize_none_metadata(self):
        """metadata=None → restored ParsedResource.metadata is None."""
        data = {
            "title": "T",
            "raw_title": "T",
            "metadata": None,
        }
        restored = deserialize_parsed_resource(data)
        assert restored.metadata is None


# ========================================================================
# DedupService integration tests
# ========================================================================

class TestDedupService:

    # --------------------------------------------------------------
    # Acceptance 2: resource_key miss → create Work + Resource
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_create_path_new_work_and_resource(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        result = await svc.dedup(
            normalized=normalized, parsed=parsed,
            raw_message_id=1, channel_id=1,
        )
        await db_session.commit()

        assert result.is_new is True
        assert result.resource_id > 0
        assert result.work_id > 0
        assert result.source_count == 1
        assert result.created_link_count == 1
        assert result.created_source is True
        assert "resource_key miss" in result.matched_reason

        work = await db_session.get(Work, result.work_id)
        assert work is not None and work.type == "drama"

        resource = await db_session.get(Resource, result.resource_id)
        assert resource is not None and resource.resource_type == "single_episode"

    # --------------------------------------------------------------
    # Acceptance: Work exists + Resource new (episode_kind)
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_create_path_existing_work(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=10, channel_id=2)

        # Pre-create a Work
        work_repo = WorkRepository(db_session)
        work = await work_repo.create(Work(
            title="家业", title_norm="家业", type="drama",
            aliases=[], year=None, work_key="drama:家业:unknown",
        ))
        await db_session.flush()

        # Dedup a NEW episode under the same work_key
        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        result = await svc.dedup(
            normalized=normalized, parsed=parsed,
            raw_message_id=10, channel_id=2,
        )
        await db_session.commit()

        assert result.is_new is True
        assert result.work_id == work.id
        assert result.source_count == 1

        work_count = (
            await db_session.execute(
                select(func.count(Work.id)).where(Work.work_key == "drama:家业:unknown")
            )
        ).scalar_one()
        assert work_count == 1

    # --------------------------------------------------------------
    # Acceptance 1: resource_key hit → merge
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_merge_path(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)
        await _seed_message(db_session, raw_message_id=2, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        # Create
        first = await svc.dedup(normalized=normalized, parsed=parsed,
                                 raw_message_id=1, channel_id=1)
        await db_session.commit()

        # Merge (same resource_key, diff raw_message)
        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=2, channel_id=1)
        await db_session.commit()

        assert result.is_new is False
        assert result.resource_id == first.resource_id
        assert "resource_key hit" in result.matched_reason
        assert result.created_link_count == 0

    # --------------------------------------------------------------
    # Acceptance 3: same link not inserted twice
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_link_dedup(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        first = await svc.dedup(normalized=normalized, parsed=parsed,
                                 raw_message_id=1, channel_id=1)
        await db_session.commit()

        # Same link, same raw_message
        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=1, channel_id=1)
        await db_session.commit()

        assert result.created_link_count == 0

        link_count = (
            await db_session.execute(
                select(func.count(ResourceLink.id)).where(
                    ResourceLink.resource_id == first.resource_id,
                )
            )
        ).scalar_one()
        assert link_count == 1

    # --------------------------------------------------------------
    # Acceptance 4: ResourceSource tracks ids
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_source_tracking(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=42, channel_id=99)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=42, channel_id=99)
        await db_session.commit()

        sources = (
            await db_session.execute(
                select(ResourceSource).where(
                    ResourceSource.resource_id == result.resource_id,
                )
            )
        ).scalars().all()

        assert len(sources) == 1
        assert sources[0].raw_message_id == 42
        assert sources[0].channel_id == 99
        assert sources[0].match_type == "title_episode"
        assert sources[0].parsed_snapshot["title"] == "家业"

    # --------------------------------------------------------------
    # Acceptance 4b: source idempotent — repeat same raw_message
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_source_idempotent(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        first = await svc.dedup(normalized=normalized, parsed=parsed,
                                 raw_message_id=1, channel_id=1)
        await db_session.commit()

        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=1, channel_id=1)
        await db_session.commit()

        assert result.created_source is False
        assert result.source_count == 1

    # --------------------------------------------------------------
    # Acceptance 5: source_count = actual COUNT
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_source_count_recalc(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)
        await _seed_message(db_session, raw_message_id=2, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        await svc.dedup(normalized=normalized, parsed=parsed,
                         raw_message_id=1, channel_id=1)
        await db_session.commit()

        await svc.dedup(normalized=normalized, parsed=parsed,
                         raw_message_id=2, channel_id=1)
        await db_session.commit()

        # Third call: repeat raw_message_id=1
        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=1, channel_id=1)
        await db_session.commit()

        assert result.source_count == 2  # two unique raw_messages

        resource = await db_session.get(Resource, result.resource_id)
        assert resource is not None and resource.source_count == 2

    # --------------------------------------------------------------
    # Acceptance 6: matched_reason human-readable
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_matched_reason_readable(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)
        await _seed_message(db_session, raw_message_id=2, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        r1 = await svc.dedup(normalized=normalized, parsed=parsed,
                              raw_message_id=1, channel_id=1)
        await db_session.commit()
        assert "resource_key miss" in r1.matched_reason
        assert normalized.resource_key in r1.matched_reason

        r2 = await svc.dedup(normalized=normalized, parsed=parsed,
                              raw_message_id=2, channel_id=1)
        await db_session.commit()
        assert "resource_key hit" in r2.matched_reason
        assert normalized.resource_key in r2.matched_reason

    # --------------------------------------------------------------
    # Acceptance 7: full idempotency — exact same call 3 times
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_power_idempotent(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        results = []
        for _ in range(3):
            r = await svc.dedup(normalized=normalized, parsed=parsed,
                                 raw_message_id=1, channel_id=1)
            await db_session.commit()
            results.append(r)

        assert results[0].is_new is True
        assert results[0].created_source is True
        assert results[0].created_link_count == 1
        assert results[0].source_count == 1

        for r in results[1:]:
            assert r.is_new is False
            assert r.created_source is False
            assert r.created_link_count == 0
            assert r.source_count == 1
            assert r.resource_id == results[0].resource_id

        assert (
            await db_session.execute(select(func.count(Resource.id)))
        ).scalar_one() == 1
        assert (
            await db_session.execute(select(func.count(ResourceLink.id)))
        ).scalar_one() == 1
        assert (
            await db_session.execute(select(func.count(ResourceSource.id)))
        ).scalar_one() == 1

    # --------------------------------------------------------------
    # Acceptance: provider stored as string
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_provider_stored_as_string(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=1, channel_id=1)
        await db_session.commit()

        links = (
            await db_session.execute(
                select(ResourceLink).where(
                    ResourceLink.resource_id == result.resource_id,
                )
            )
        ).scalars().all()
        assert len(links) == 1
        assert isinstance(links[0].provider, str)
        assert links[0].provider == "quark"

    # --------------------------------------------------------------
    # Acceptance: Resource.resource_type = episode_kind
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_resource_type_is_episode_kind(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)

        svc = DedupService(db_session)
        parsed = _make_parsed_resource()
        normalized = _make_normalized(parsed)

        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=1, channel_id=1)
        await db_session.commit()

        resource = await db_session.get(Resource, result.resource_id)
        assert resource is not None
        assert resource.resource_type == "single_episode"
        work = await db_session.get(Work, resource.work_id)
        assert work is not None and work.type == "drama"

    # --------------------------------------------------------------
    # Multi-link create + dedup
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_multi_link_create(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)
        await _seed_message(db_session, raw_message_id=2, channel_id=1)

        links = [
            ParsedLink(provider=LinkProvider.QUARK, original_text="夸克 https://pan.quark.cn/s/abc",
                        url="https://pan.quark.cn/s/abc", share_id="abc", link_type="url"),
            ParsedLink(provider=LinkProvider.BAIDU, original_text="百度 https://pan.baidu.com/s/def",
                        url="https://pan.baidu.com/s/def", share_id="def", link_type="url"),
        ]
        parsed = _make_parsed_resource(links=links)
        normalized = _make_normalized(parsed)

        svc = DedupService(db_session)
        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=1, channel_id=1)
        await db_session.commit()
        assert result.created_link_count == 2

        result2 = await svc.dedup(normalized=normalized, parsed=parsed,
                                   raw_message_id=2, channel_id=1)
        await db_session.commit()
        assert result2.created_link_count == 0
        assert result2.source_count == 2

    # --------------------------------------------------------------
    # Xunlei command link
    # --------------------------------------------------------------
    @pytest.mark.asyncio
    async def test_xunlei_command_link(self, db_session: AsyncSession):
        await _seed_message(db_session, raw_message_id=1, channel_id=1)
        await _seed_message(db_session, raw_message_id=2, channel_id=1)

        links = [
            ParsedLink(provider=LinkProvider.XUNLEI, original_text="迅雷口令：xunlei123",
                        url=None, password="xunlei123", link_type="command"),
        ]
        parsed = _make_parsed_resource(links=links)
        normalized = _make_normalized(parsed)

        svc = DedupService(db_session)
        result = await svc.dedup(normalized=normalized, parsed=parsed,
                                  raw_message_id=1, channel_id=1)
        await db_session.commit()
        assert result.created_link_count == 1

        links2 = (
            await db_session.execute(
                select(ResourceLink).where(
                    ResourceLink.resource_id == result.resource_id,
                )
            )
        ).scalars().all()
        assert len(links2) == 1
        assert links2[0].original_url is None

        result2 = await svc.dedup(normalized=normalized, parsed=parsed,
                                   raw_message_id=2, channel_id=1)
        await db_session.commit()
        assert result2.created_link_count == 0
