"""P2-C tests — persist ParserPipeline outcomes on RawMessage."""

from datetime import datetime

import pytest

from app.modules.channel.schema import ChannelCreate
from app.modules.channel.service import ChannelService
from app.modules.parser.pipeline.core import PARSER_VERSION, RULE_VERSION
from app.modules.rawmessage.schema import RawMessageCreate
from app.modules.rawmessage.service import EMPTY_PARSE_ERROR, RawMessageService


async def _ingest_message(db_session, raw_text: str):
    channel = await ChannelService(db_session).create_or_get(
        ChannelCreate(name="P2-C 测试频道", tg_id=72001)
    )
    return await RawMessageService(db_session).ingest(
        RawMessageCreate(
            channel_id=channel.id,
            tg_message_id=72001,
            raw_text=raw_text,
        )
    )


@pytest.mark.asyncio
async def test_parse_success_persists_result_and_versions(db_session):
    message = await _ingest_message(
        db_session,
        "【家业】第1集 1080p 夸克 https://pan.quark.cn/s/p2c001",
    )

    parsed = await RawMessageService(db_session).parse_and_persist(message.id)

    assert parsed.parse_status == "parsed"
    assert parsed.parse_attempts == 1
    assert parsed.parser_version == PARSER_VERSION
    assert parsed.rule_version == RULE_VERSION
    assert parsed.last_parse_error is None
    assert isinstance(parsed.last_parsed_at, datetime)
    assert isinstance(parsed.parsed_data, list)
    assert len(parsed.parsed_data) == 1
    assert parsed.parsed_data[0]["title"] == "家业"
    assert parsed.parsed_data[0]["parser_version"] == PARSER_VERSION
    assert parsed.parsed_data[0]["rule_version"] == RULE_VERSION


@pytest.mark.asyncio
async def test_empty_parse_result_persists_failure(db_session):
    message = await _ingest_message(db_session, "只有标题但没有任何网盘链接")

    parsed = await RawMessageService(db_session).parse_and_persist(message.id)

    assert parsed.parse_status == "parse_failed"
    assert parsed.parsed_data is None
    assert parsed.parser_version == PARSER_VERSION
    assert parsed.rule_version == RULE_VERSION
    assert parsed.parse_attempts == 1
    assert parsed.last_parse_error == EMPTY_PARSE_ERROR
    assert parsed.last_parsed_at is not None


@pytest.mark.asyncio
async def test_pipeline_exception_persists_failure(db_session):
    class FailingPipeline:
        async def parse(self, raw_text: str, raw_message_id: int | None = None):
            raise RuntimeError("broken parser rule")

    message = await _ingest_message(
        db_session,
        "家业 夸克 https://pan.quark.cn/s/p2c002",
    )
    service = RawMessageService(db_session, parser_pipeline=FailingPipeline())

    parsed = await service.parse_and_persist(message.id)

    assert parsed.parse_status == "parse_failed"
    assert parsed.parsed_data is None
    assert parsed.parse_attempts == 1
    assert parsed.last_parse_error == "RuntimeError: broken parser rule"
    assert parsed.last_parsed_at is not None


@pytest.mark.asyncio
async def test_successful_retry_clears_previous_error(db_session):
    message = await _ingest_message(db_session, "家业 暂无链接")
    service = RawMessageService(db_session)

    first_attempt = await service.parse_and_persist(message.id)
    first_parsed_at = first_attempt.last_parsed_at
    assert first_attempt.parse_status == "parse_failed"
    assert first_attempt.parse_attempts == 1

    first_attempt.raw_text = (
        "家业 第2集 百度 https://pan.baidu.com/s/p2c003 提取码 retry"
    )
    await db_session.commit()

    second_attempt = await service.parse_and_persist(message.id)

    assert second_attempt.parse_status == "parsed"
    assert second_attempt.parse_attempts == 2
    assert second_attempt.last_parse_error is None
    assert second_attempt.parsed_data[0]["title"] == "家业"
    assert second_attempt.last_parsed_at >= first_parsed_at


@pytest.mark.asyncio
async def test_missing_raw_message_raises_lookup_error(db_session):
    with pytest.raises(LookupError, match="RawMessage id=999999 not found"):
        await RawMessageService(db_session).parse_and_persist(999999)
