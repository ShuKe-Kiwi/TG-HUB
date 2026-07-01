"""P3-A tests for pure Normalizer + Fingerprint functions."""

import hashlib
from dataclasses import FrozenInstanceError

import pytest

from app.modules.normalizer import (
    build_content_fingerprint,
    build_episode_key,
    build_resource_key,
    build_work_key,
    normalize_episode,
    normalize_resource,
    normalize_title,
)
from app.modules.parser.dto import ParsedMetadata, ParsedResource


@pytest.mark.parametrize(
    ("title", "expected_title", "expected_range"),
    [
        ("【家业】第一集", "家业", "ep1"),
        ("家业 EP01", "家业", "ep1"),
        ("家业 01", "家业", "ep1"),
        ("家业 更新至10集", "家业", "ep1-10"),
        ("家业 全集", "家业", "all"),
        ("家业 第1-5集", "家业", "ep1-5"),
    ],
)
def test_architecture_normalization_examples(
    title,
    expected_title,
    expected_range,
):
    assert normalize_title(title) == expected_title
    assert normalize_episode(title).episode_range == expected_range


def test_title_nfkc_casefold_whitespace_and_decorations():
    assert normalize_title("【ＴＥＳＴ　Show】") == "test show"


@pytest.mark.parametrize(
    "title",
    [
        "资源：家业",
        "分享 家业",
        "更新 - 家业",
    ],
)
def test_only_independent_leading_noise_prefixes_are_removed(title):
    assert normalize_title(title) == "家业"


@pytest.mark.parametrize(
    "title",
    [
        "更新换代",
        "更新至10集的家业",
        "资源管理器",
    ],
)
def test_valid_title_content_is_not_over_deleted(title):
    assert normalize_title(title) == title


def test_metadata_takes_precedence_and_records_source():
    result = normalize_episode(
        "家业 EP02",
        ParsedMetadata(episode_no=1),
    )

    assert result.episode_range == "ep1"
    assert result.episode_no == 1
    assert result.episode_source == "metadata"


def test_title_inference_records_source():
    result = normalize_episode("家业 第2集")

    assert result.episode_range == "ep2"
    assert result.episode_source == "title"


def test_unknown_episode_records_unknown_source():
    result = normalize_episode("家业")

    assert result.episode_kind == "unknown"
    assert result.episode_range == "unknown"
    assert result.episode_source == "unknown"


@pytest.mark.parametrize(
    "title",
    [
        "家业 2026",
        "家业 1080p",
        "家业 12.5GB",
        "家业 2026 01",
        "家业 1080p 01",
        "家业 12.5GB 01",
        "家业 00",
        "家业 1",
    ],
)
def test_trailing_number_negative_cases(title):
    assert normalize_episode(title).episode_kind == "unknown"


def test_season_single_episode_is_canonical():
    result = normalize_episode("家业 S02E01")

    assert result.episode_kind == "single_episode"
    assert result.season_no == 2
    assert result.episode_no == 1
    assert result.episode_range == "s02e1"


def test_season_episode_range_is_canonical():
    result = normalize_episode("家业 S02E05-10")

    assert result.episode_kind == "episode_range"
    assert result.season_no == 2
    assert result.episode_no is None
    assert result.episode_range == "s02e5-10"


def test_english_episode_range_is_canonical():
    result = normalize_episode("家业 EP01-05")

    assert result.episode_kind == "episode_range"
    assert result.episode_no is None
    assert result.episode_range == "ep1-5"
    assert normalize_title("家业 EP01-05") == "家业"


def test_chinese_season_and_episode_are_canonical():
    result = normalize_episode("家业 第二季 第十二集")

    assert result.episode_kind == "single_episode"
    assert result.season_no == 2
    assert result.episode_no == 12
    assert result.episode_range == "s02e12"


def test_full_always_uses_all_and_has_no_episode_key():
    normalized = normalize_resource(
        ParsedResource(title="家业 全集", raw_title="家业 全集")
    )

    assert normalized.content_type == "drama"
    assert normalized.episode_kind == "full"
    assert normalized.episode_range == "all"
    assert normalized.resource_key == "drama:家业:all"
    assert normalized.episode_key is None


def test_unknown_episode_uses_unknown_in_resource_key():
    normalized = normalize_resource(
        ParsedResource(title="家业", raw_title="家业")
    )

    assert normalized.episode_kind == "unknown"
    assert normalized.episode_range == "unknown"
    assert normalized.resource_key == "drama:家业:unknown"
    assert normalized.episode_key is None


def test_content_type_and_episode_kind_are_not_mixed():
    normalized = normalize_resource(
        ParsedResource(
            title="家业 第1集",
            raw_title="家业 第1集",
            resource_type="movie",
        )
    )

    assert normalized.content_type == "movie"
    assert normalized.episode_kind == "single_episode"
    assert normalized.resource_key == "movie:家业:ep1"


def test_invalid_content_type_falls_back_to_other():
    normalized = normalize_resource(
        ParsedResource(
            title="家业 全集",
            raw_title="家业 全集",
            resource_type="full",
        )
    )

    assert normalized.content_type == "other"
    assert normalized.episode_kind == "full"


def test_key_builders_follow_architecture_contract():
    assert build_work_key("drama", "家业", 2026) == "drama:家业:2026"
    assert build_work_key("drama", "家业", None) == "drama:家业:unknown"
    assert build_resource_key("drama", "家业", "ep1-10") == (
        "drama:家业:ep1-10"
    )
    assert build_episode_key(
        "drama",
        "家业",
        "single_episode",
        None,
        1,
    ) == "drama:家业:s01:e01"


@pytest.mark.parametrize(
    "episode_kind",
    ["episode_range", "full", "unknown"],
)
def test_episode_key_is_none_for_non_single_episode(episode_kind):
    assert (
        build_episode_key(
            "drama",
            "家业",
            episode_kind,
            None,
            None,
        )
        is None
    )


@pytest.mark.parametrize(
    "episode_range",
    ["full", "ep0", "ep5-1", "s2e1", "s00e1", "episode1", ""],
)
def test_resource_key_rejects_noncanonical_episode_range(episode_range):
    with pytest.raises(ValueError, match="invalid canonical episode_range"):
        build_resource_key("drama", "家业", episode_range)


def test_content_fingerprint_uses_stable_documented_payload():
    expected = hashlib.sha256(
        "家业ep1drama2026".encode("utf-8")
    ).hexdigest()

    actual = build_content_fingerprint("家业", "ep1", "drama", 2026)

    assert actual == expected
    assert len(actual) == 64
    assert actual == build_content_fingerprint(
        "家业",
        "ep1",
        "drama",
        2026,
    )


def test_content_fingerprint_changes_with_identity_fields():
    first = build_content_fingerprint("家业", "ep1", "drama", 2026)
    second = build_content_fingerprint("家业", "ep2", "drama", 2026)

    assert first != second


def test_normalize_resource_builds_all_identity_fields():
    normalized = normalize_resource(
        ParsedResource(
            title="【家业】第一集",
            raw_title="【家业】第一集 1080p",
            resource_type="drama",
            metadata=ParsedMetadata(
                episode_no=1,
                year=2026,
            ),
        )
    )

    assert normalized.title_norm == "家业"
    assert normalized.content_type == "drama"
    assert normalized.year == 2026
    assert normalized.episode_kind == "single_episode"
    assert normalized.episode_source == "metadata"
    assert normalized.episode_range == "ep1"
    assert normalized.work_key == "drama:家业:2026"
    assert normalized.resource_key == "drama:家业:ep1"
    assert normalized.episode_key == "drama:家业:s01:e01"
    assert len(normalized.content_fingerprint) == 64


def test_normalize_resource_does_not_mutate_input():
    resource = ParsedResource(
        title="【家业】第一集",
        raw_title="原始标题",
        metadata=ParsedMetadata(episode_no=1, year=2026),
        tags=["HDR"],
    )
    before = resource.model_dump(mode="json")

    normalized = normalize_resource(resource)

    assert resource.model_dump(mode="json") == before
    with pytest.raises(FrozenInstanceError):
        normalized.title_norm = "被修改"


def test_empty_normalized_title_is_rejected():
    with pytest.raises(ValueError, match="title_norm must not be empty"):
        normalize_title("【】 ★")
