import json
from pathlib import Path

import pytest

from app.modules.monitor.config import WatchlistConfig, load_watchlist
from app.modules.monitor.source_channels import precheck_source_channels


WATCHLIST_PATH = Path("/Users/kiwishook/.tg-hub/watchlist.json")
_MISSING_EXTERNAL_CONFIG = "external ~/.tg-hub monitor config is not present"


@pytest.mark.skipif(
    not WATCHLIST_PATH.exists(),
    reason=_MISSING_EXTERNAL_CONFIG,
)
def test_p6_2c_0_external_watchlist_source_channels_are_prechecked() -> None:
    watchlist = load_watchlist(WATCHLIST_PATH)

    results = precheck_source_channels(watchlist)

    assert len(results) == 2
    assert [result.input_type for result in results] == ["tme_url", "tme_url"]
    assert [result.status for result in results] == [
        "username_requires_resolution",
        "username_requires_resolution",
    ]
    assert [result.numeric_channel_id for result in results] == [None, None]

    serialized = json.dumps(
        [result.model_dump() for result in results],
        ensure_ascii=False,
    )
    assert "dmysfx" not in serialized
    assert "Aliyun_4K_Movies" not in serialized


def test_precheck_source_channels_handles_allowed_ref_forms() -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "123456"},
            {"ref": "-100987654321"},
            {"ref": "@Valid_Channel_01"},
            {"ref": "https://t.me/some_channel"},
            {"ref": "https://example.com/not-telegram"},
            {"ref": "https://t.me/disabled_channel", "enabled": False},
        ],
        watch_titles=[],
    )

    results = precheck_source_channels(watchlist)

    assert [result.input_type for result in results] == [
        "numeric_id",
        "numeric_id",
        "username",
        "tme_url",
        "invalid",
    ]
    assert [result.status for result in results] == [
        "parsed_numeric_id",
        "parsed_numeric_id",
        "username_requires_resolution",
        "username_requires_resolution",
        "invalid_ref",
    ]
    assert [result.numeric_channel_id for result in results] == [
        123456,
        -100987654321,
        None,
        None,
        None,
    ]


def test_precheck_source_channels_output_is_desensitized() -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "@Sensitive_Channel"},
            {"ref": "https://t.me/another_secret"},
            {"ref": "-1001234567890"},
        ],
        watch_titles=[],
    )

    results = precheck_source_channels(watchlist)
    serialized = json.dumps(
        [result.model_dump() for result in results],
        ensure_ascii=False,
    )

    assert "Sensitive_Channel" not in serialized
    assert "another_secret" not in serialized
    assert all(result.masked_ref for result in results)
    assert results[2].numeric_channel_id == -1001234567890
    assert results[2].masked_ref == "-1***90"
