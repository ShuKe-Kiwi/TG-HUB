import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from app.config import Settings
from app.modules.monitor.config import (
    WatchlistConfig,
    load_watchlist,
)
from app.modules.monitor.filter import filter_message, normalize_watch_text
from app.modules.monitor.schema import IncomingMessage


WATCHLIST_PATH = Path("/Users/kiwishook/.tg-hub/watchlist.json")
LISTENING_CHANNEL_SAMPLES_PATH = Path(
    "/Users/kiwishook/.tg-hub/p6_2b_samples.json"
)
_MISSING_EXTERNAL_CONFIG = "external ~/.tg-hub monitor config is not present"


class ListeningChannelSample(BaseModel):
    sample_id: str
    text: str | None = None
    caption: str | None = None
    expected_matched: bool
    expected_watch_titles: list[str] = Field(default_factory=list)
    expected_parser_valid: bool | None = None
    category: str

    @property
    def message_text(self) -> str:
        return self.text or self.caption or ""


def load_listening_channel_samples(
    path: Path,
) -> list[ListeningChannelSample]:
    raw_data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw_data, list)
    return [ListeningChannelSample.model_validate(item) for item in raw_data]


def _sample_to_message(
    sample: ListeningChannelSample,
    *,
    message_id: int = 1,
) -> IncomingMessage:
    return IncomingMessage(
        source_ref="offline://p6_2b_samples",
        source_message_id=message_id,
        text=sample.text,
        caption=sample.caption,
        raw_payload={"sample_id": sample.sample_id},
        published_at=None,
    )


def _fixture_watchlist(
    samples: list[ListeningChannelSample],
) -> WatchlistConfig:
    titles = dict.fromkeys(
        title
        for sample in samples
        for title in sample.expected_watch_titles
    )
    return WatchlistConfig(
        source_channels=[],
        watch_titles=[{"title": title} for title in titles],
    )


def test_settings_exposes_default_monitor_config_paths() -> None:
    configured = Settings()

    assert configured.WATCHLIST_PATH == Path("~/.tg-hub/watchlist.json")


@pytest.mark.skipif(
    not WATCHLIST_PATH.exists(),
    reason=_MISSING_EXTERNAL_CONFIG,
)
def test_load_watchlist_validates_external_config_file() -> None:
    watchlist = load_watchlist(WATCHLIST_PATH)

    assert isinstance(watchlist, WatchlistConfig)
    assert watchlist.enabled_source_refs()
    assert watchlist.enabled_watch_titles()


@pytest.mark.skipif(
    not LISTENING_CHANNEL_SAMPLES_PATH.exists(),
    reason=_MISSING_EXTERNAL_CONFIG,
)
def test_load_listening_channel_samples_validates_external_config() -> None:
    samples = load_listening_channel_samples(LISTENING_CHANNEL_SAMPLES_PATH)

    assert len(samples) == 12
    assert samples[0].sample_id == "sample_001_exact_match"
    assert samples[10].message_text == (
        "百花杀 第1集 百度：https://pan.baidu.com/s/p6b011?pwd=ijkl"
    )


@pytest.mark.skipif(
    not LISTENING_CHANNEL_SAMPLES_PATH.exists(),
    reason=_MISSING_EXTERNAL_CONFIG,
)
def test_p6_2b_offline_samples_filter_by_watch_titles_only() -> None:
    samples = load_listening_channel_samples(LISTENING_CHANNEL_SAMPLES_PATH)
    watchlist = _fixture_watchlist(samples)

    results = []
    for index, sample in enumerate(samples, start=1):
        result = filter_message(_sample_to_message(sample, message_id=index), watchlist)
        results.append((sample, result))

    assert [
        result.matched for _, result in results
    ] == [sample.expected_matched for sample in samples]
    assert [
        result.matched_titles for _, result in results
    ] == [sample.expected_watch_titles for sample in samples]


@pytest.mark.skipif(
    not LISTENING_CHANNEL_SAMPLES_PATH.exists(),
    reason=_MISSING_EXTERNAL_CONFIG,
)
def test_p6_2b_acceptance_report_counts() -> None:
    samples = load_listening_channel_samples(LISTENING_CHANNEL_SAMPLES_PATH)
    watchlist = _fixture_watchlist(samples)

    results = [
        filter_message(_sample_to_message(sample, message_id=index), watchlist)
        for index, sample in enumerate(samples, start=1)
    ]

    expected_match_passed = sum(
        result.matched is sample.expected_matched
        for sample, result in zip(samples, results, strict=True)
    )
    expected_titles_passed = sum(
        result.matched_titles == sample.expected_watch_titles
        for sample, result in zip(samples, results, strict=True)
    )
    report = {
        "watchlist_schema": "pass",
        "sample_schema": "pass",
        "source_channel_entries": len(watchlist.source_channels),
        "watch_title_entries": len(watchlist.watch_titles),
        "sample_count": len(samples),
        "matched_count": sum(result.matched for result in results),
        "unmatched_count": sum(not result.matched for result in results),
        "multi_match_count": sum(
            len(result.matched_titles) > 1 for result in results
        ),
        "expected_match_passed": expected_match_passed,
        "expected_match_failed": len(samples) - expected_match_passed,
        "expected_titles_passed": expected_titles_passed,
        "expected_titles_failed": len(samples) - expected_titles_passed,
        "text_selected_count": sum(
            bool(sample.text and sample.text.strip()) for sample in samples
        ),
        "caption_fallback_count": sum(
            not bool(sample.text and sample.text.strip())
            and bool(sample.caption and sample.caption.strip())
            for sample in samples
        ),
        "empty_content_count": sum(
            result.reason == "empty_content" for result in results
        ),
        "false_positive_count": sum(
            result.matched and not sample.expected_matched
            for sample, result in zip(samples, results, strict=True)
        ),
        "false_negative_count": sum(
            not result.matched and sample.expected_matched
            for sample, result in zip(samples, results, strict=True)
        ),
        "telegram_api_accessed": "no",
        "database_accessed": "no",
        "parser_called": "no",
        "dedup_called": "no",
        "notification_sent": "no",
    }

    assert report["sample_count"] == 12
    assert report["expected_match_failed"] == 0
    assert report["expected_titles_failed"] == 0
    assert report["false_positive_count"] == 0
    assert report["false_negative_count"] == 0


def test_watchlist_loader_can_read_equivalent_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.json"
    path.write_text(
        json.dumps(
            {
                "source_channels": [{"ref": "https://t.me/example"}],
                "watch_titles": [{"title": "机动新世纪高达X"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    watchlist = load_watchlist(path)

    result = filter_message(
        IncomingMessage(
            source_ref="offline://unit",
            source_message_id=1,
            text="【机动新世纪高达Ｘ】（1996）1080P 全39集",
        ),
        watchlist,
    )

    assert result.matched is True
    assert result.reason == "matched"
    assert result.matched_titles == ["机动新世纪高达X"]


def test_incoming_message_content_text_selection() -> None:
    assert (
        IncomingMessage(
            source_ref="offline://unit",
            source_message_id=1,
            text=" 家业 ",
            caption="百花杀",
        ).content_text
        == "家业"
    )
    assert (
        IncomingMessage(
            source_ref="offline://unit",
            source_message_id=2,
            text="   ",
            caption=" 百花杀 ",
        ).content_text
        == "百花杀"
    )
    assert (
        IncomingMessage(
            source_ref="offline://unit",
            source_message_id=3,
            text=" ",
            caption=None,
        ).content_text
        == ""
    )


def test_filter_reason_boundaries() -> None:
    empty_watchlist = WatchlistConfig(
        source_channels=[{"ref": "https://t.me/ignored"}],
        watch_titles=[],
    )
    watchlist = WatchlistConfig(
        source_channels=[],
        watch_titles=[{"title": "家业"}],
    )

    empty_content = filter_message(
        IncomingMessage(
            source_ref="offline://unit",
            source_message_id=1,
            text=" ",
        ),
        watchlist,
    )
    no_watch_titles = filter_message(
        IncomingMessage(
            source_ref="offline://unit",
            source_message_id=2,
            text="家业",
        ),
        empty_watchlist,
    )
    no_title_match = filter_message(
        IncomingMessage(
            source_ref="offline://unit",
            source_message_id=3,
            text="唐朝诡事录",
        ),
        watchlist,
    )

    assert empty_content.reason == "empty_content"
    assert no_watch_titles.reason == "no_watch_titles"
    assert no_title_match.reason == "no_title_match"


def test_source_channels_do_not_participate_in_p6_2b_filtering() -> None:
    watchlist = WatchlistConfig(
        source_channels=[{"ref": "https://t.me/allowed"}],
        watch_titles=[{"title": "家业"}],
    )
    result = filter_message(
        IncomingMessage(
            source_ref="offline://not-in-source-channels",
            source_message_id=1,
            text="家业 第1集",
        ),
        watchlist,
    )

    assert result.matched is True
    assert result.matched_titles == ["家业"]


def test_p6_2b_normalization_scope() -> None:
    assert normalize_watch_text("【机动新世纪高达Ｘ】（1996）4K 全39集") == (
        "机动新世纪高达x"
    )
