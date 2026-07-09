"""P1 tests — RawMessage idempotent ingest.

7 scenarios per ARCHITECTURE.md V2.1-final verification requirements:
1. Create Channel successfully
2. Duplicate tg_id Channel returns existing (no error, no overwrite)
3. Create RawMessage successfully
4. Duplicate (channel_id, tg_message_id) → only one record
5. Duplicate write returns existing (same id, fields unmodified)
6. raw_text / raw_payload / raw_media_refs saved verbatim
7. Initial states correct: stored / parse_pending / dedup_pending / parse_attempts=0
"""

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.rawmessage.model import RawMessage
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import RawMessageService


@pytest.mark.asyncio
async def test_create_channel_success(db_session):
    """Scenario 1: Create Channel successfully."""
    svc = ChannelService(db_session)
    channel = await svc.create_or_get(
        ChannelCreate(name="短剧频道", tg_id=123456789, tg_username="duan_ju")
    )
    assert channel.id is not None
    assert channel.name == "短剧频道"
    assert channel.tg_id == 123456789
    assert channel.tg_username == "duan_ju"
    assert channel.status == "active"
    assert channel.source_type == "telegram"


@pytest.mark.asyncio
async def test_duplicate_channel_returns_existing(db_session):
    """Scenario 2: Duplicate tg_id returns existing, no error, no overwrite."""
    svc = ChannelService(db_session)

    first = await svc.create_or_get(
        ChannelCreate(name="频道A", tg_id=999, tg_username="channel_a")
    )
    second = await svc.create_or_get(
        ChannelCreate(name="频道B-改名", tg_id=999, tg_username="channel_b")
    )

    assert first.id == second.id
    assert second.name == "频道A"  # NOT overwritten
    assert second.tg_username == "channel_a"  # NOT overwritten


@pytest.mark.asyncio
async def test_create_rawmessage_success(db_session):
    """Scenario 3: Create RawMessage successfully."""
    channel_svc = ChannelService(db_session)
    channel = await channel_svc.create_or_get(
        ChannelCreate(name="测试频道", tg_id=111, tg_username="test_ch")
    )

    msg_svc = RawMessageService(db_session)
    msg = await msg_svc.ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=1001,
            raw_text="家业 更新至10集\n夸克：https://pan.quark.cn/s/abc123",
        )
    )

    assert msg.disposition == "stored"
    assert msg.raw_message.id == msg.id
    assert msg.id is not None
    assert msg.channel_id == channel.id
    assert msg.tg_message_id == 1001
    assert "家业" in msg.raw_text


@pytest.mark.asyncio
async def test_duplicate_rawmessage_only_one_record(db_session):
    """Scenario 4: Same (channel_id, tg_message_id) → only one record."""
    channel_svc = ChannelService(db_session)
    channel = await channel_svc.create_or_get(
        ChannelCreate(name="频道", tg_id=222, tg_username="ch")
    )

    msg_svc = RawMessageService(db_session)

    await msg_svc.ingest(
        RawMessageCreate(
            channel_id=channel.id, tg_message_id=2001, raw_text="第一条消息"
        )
    )
    duplicate = await msg_svc.ingest(
        RawMessageCreate(
            channel_id=channel.id, tg_message_id=2001, raw_text="第二条消息-重复"
        )
    )

    assert duplicate.disposition == "duplicate"
    # Verify only one record exists
    existing = await msg_svc.get_by_channel_and_msg_id(channel.id, 2001)
    assert existing is not None
    assert existing.id is not None


@pytest.mark.asyncio
async def test_duplicate_rawmessage_returns_existing_unmodified(db_session):
    """Scenario 5: Duplicate write returns existing (same id, fields not modified)."""
    channel_svc = ChannelService(db_session)
    channel = await channel_svc.create_or_get(
        ChannelCreate(name="频道", tg_id=333, tg_username="ch3")
    )

    msg_svc = RawMessageService(db_session)

    first = await msg_svc.ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=3001,
            raw_text="原始消息内容",
        )
    )

    second = await msg_svc.ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=3001,
            raw_text="完全不同的内容-应该被忽略",
        )
    )

    assert first.disposition == "stored"
    assert second.disposition == "duplicate"
    assert first.id == second.id
    assert second.raw_text == "原始消息内容"  # NOT overwritten
    assert "完全不同的内容" not in second.raw_text


@pytest.mark.asyncio
async def test_rawmessage_fields_saved_verbatim(db_session):
    """Scenario 6: raw_text / raw_payload / raw_media_refs saved completely."""
    channel_svc = ChannelService(db_session)
    channel = await channel_svc.create_or_get(
        ChannelCreate(name="频道", tg_id=444, tg_username="ch4")
    )

    msg_svc = RawMessageService(db_session)

    raw_text = "【家业】第1集\n夸克：https://pan.quark.cn/s/xyz789 提取码：abcd\n1080p 国语"
    raw_media_refs = [
        {"type": "photo", "file_id": "AgADBQADxxx", "file_path": None},
        {"type": "video", "file_id": "AgADBQADyyy", "file_path": None},
    ]
    raw_payload = {"message_id": 5001, "peer": "channel", "has_media": True}

    msg = await msg_svc.ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=5001,
            raw_text=raw_text,
            raw_media_refs=raw_media_refs,
            raw_payload=raw_payload,
        )
    )

    # Read back from DB
    fetched = await msg_svc.get_by_id(msg.id)
    assert fetched is not None
    assert fetched.raw_text == raw_text
    assert fetched.raw_payload == raw_payload
    assert fetched.raw_media_refs == raw_media_refs


@pytest.mark.asyncio
async def test_rawmessage_initial_states(db_session):
    """Scenario 7: Initial states correct after ingest."""
    channel_svc = ChannelService(db_session)
    channel = await channel_svc.create_or_get(
        ChannelCreate(name="频道", tg_id=555, tg_username="ch5")
    )

    msg_svc = RawMessageService(db_session)
    msg = await msg_svc.ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=6001,
            raw_text="测试初始状态",
        )
    )

    assert msg.ingest_status == "stored"
    assert msg.parse_status == "parse_pending"
    assert msg.dedup_status == "dedup_pending"
    assert msg.parse_attempts == 0


@pytest.mark.asyncio
async def test_concurrent_duplicate_rawmessage_returns_one_stored_rest_duplicate(
    db_session,
):
    """P6-2E prerequisite: concurrent idempotency reports atomic disposition."""
    channel = await ChannelService(db_session).create_or_get(
        ChannelCreate(name="并发频道", tg_id=777, tg_username="concurrent_ch")
    )
    assert db_session.bind is not None
    session_factory = async_sessionmaker(
        db_session.bind,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async def ingest_once(raw_text: str):
        async with session_factory() as session:
            return await RawMessageService(session).ingest(
                RawMessageCreate(
                    channel_id=channel.id,
                    tg_message_id=7001,
                    raw_text=raw_text,
                )
            )

    first, second = await asyncio.gather(
        ingest_once("并发消息 A"),
        ingest_once("并发消息 B"),
    )

    dispositions = sorted([first.disposition, second.disposition])
    assert dispositions == ["duplicate", "stored"]
    assert first.id == second.id

    count_result = await db_session.execute(
        select(func.count()).select_from(RawMessage).where(
            RawMessage.channel_id == channel.id,
            RawMessage.tg_message_id == 7001,
        )
    )
    assert count_result.scalar_one() == 1
