"""P3-B integration tests for the pure Parser -> Normalizer chain."""

import re

import pytest

from app.modules.normalizer import NormalizedResource, normalize_resource
from app.modules.parser.dto import ParsedResource
from app.modules.parser.pipeline.core import ParserPipeline
from tests.parser.fixtures import SAMPLE_RAW_MESSAGES


async def _parse_and_normalize(
    raw_text: str | None,
) -> tuple[list[ParsedResource], list[NormalizedResource]]:
    parsed_resources = await ParserPipeline().parse(raw_text)
    normalized_resources = [
        normalize_resource(resource) for resource in parsed_resources
    ]
    return parsed_resources, normalized_resources


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sample_index",
    range(len(SAMPLE_RAW_MESSAGES)),
)
async def test_all_fixtures_produce_stable_normalized_identity(sample_index):
    sample = SAMPLE_RAW_MESSAGES[sample_index]

    first_parsed, first_normalized = await _parse_and_normalize(
        sample["raw_text"]
    )
    second_parsed, second_normalized = await _parse_and_normalize(
        sample["raw_text"]
    )

    assert len(first_parsed) == sample["expected_result_count"]
    assert len(first_normalized) == sample["expected_result_count"]
    assert len(second_parsed) == sample["expected_result_count"]
    assert first_normalized == second_normalized

    for parsed, normalized in zip(first_parsed, first_normalized):
        assert isinstance(parsed, ParsedResource)
        assert isinstance(normalized, NormalizedResource)
        assert normalized.title_norm
        assert normalized.content_type == parsed.resource_type
        assert normalized.work_key == (
            f"{normalized.content_type}:{normalized.title_norm}:"
            f"{normalized.year if normalized.year is not None else 'unknown'}"
        )
        assert normalized.resource_key == (
            f"{normalized.content_type}:{normalized.title_norm}:"
            f"{normalized.episode_range}"
        )
        assert re.fullmatch(
            r"[0-9a-f]{64}",
            normalized.content_fingerprint,
        )

        if normalized.episode_kind == "single_episode":
            assert normalized.episode_key is not None
        else:
            assert normalized.episode_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_text",
    [
        "",
        "   \n\n\t ",
        "只有标题但没有网盘链接",
        "https://pan.quark.cn/s/no-title",
        "提取码 only123",
        None,
    ],
)
async def test_invalid_inputs_produce_no_parsed_resource_or_fake_key(raw_text):
    parsed_resources, normalized_resources = await _parse_and_normalize(
        raw_text
    )

    assert parsed_resources == []
    assert normalized_resources == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sample_index", "resource_key", "episode_key"),
    [
        (0, "drama:家业:ep1", "drama:家业:s01:e01"),
        (1, "drama:家业:ep1-10", None),
        (2, "drama:家业:unknown", None),
    ],
)
async def test_representative_fixture_keys_are_exact(
    sample_index,
    resource_key,
    episode_key,
):
    _, normalized_resources = await _parse_and_normalize(
        SAMPLE_RAW_MESSAGES[sample_index]["raw_text"]
    )

    assert len(normalized_resources) == 1
    normalized = normalized_resources[0]
    assert normalized.work_key == "drama:家业:unknown"
    assert normalized.resource_key == resource_key
    assert normalized.episode_key == episode_key
