import json

import pytest

from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.resolver import (
    ChannelResolveError,
    ChannelResolveResult,
    resolve_source_channels,
)


class FakeChannelResolver:
    def __init__(
        self,
        results: dict[str, ChannelResolveResult | Exception] | None = None,
    ) -> None:
        self.results = results or {}
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, ref: str, *, input_type: str):
        self.calls.append((ref, input_type))
        result = self.results[ref]
        if isinstance(result, Exception):
            raise result
        return result


def _resolved(
    ref: str,
    *,
    input_type: str = "username",
    channel_id: int = -100123,
    username: str = "secret_channel",
    title: str = "Secret Channel Title",
) -> ChannelResolveResult:
    return ChannelResolveResult(
        input_ref=ref,
        input_type=input_type,
        status="resolved",
        numeric_channel_id=channel_id,
        username=username,
        title=title,
    )


@pytest.mark.asyncio
async def test_numeric_and_invalid_refs_do_not_call_resolver() -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "-1001234567890"},
            {"ref": "https://example.com/not-telegram"},
            {"ref": "https://t.me/disabled_channel", "enabled": False},
        ],
        watch_titles=[],
    )
    resolver = FakeChannelResolver()

    report = await resolve_source_channels(watchlist, resolver)

    assert resolver.calls == []
    assert [item.status for item in report.results] == [
        "already_numeric",
        "invalid_ref",
    ]
    assert report.already_numeric_count == 1
    assert report.invalid_ref_count == 1
    assert report.enabled_source_channels == 2
    assert report.result_count_matches_enabled_count is True
    assert report.allow_P6_2C == "no"


@pytest.mark.asyncio
async def test_username_and_tme_url_resolve_successfully() -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "@Secret_Channel"},
            {"ref": "https://t.me/another_secret"},
        ],
        watch_titles=[],
    )
    resolver = FakeChannelResolver(
        {
            "@Secret_Channel": _resolved(
                "@Secret_Channel",
                input_type="username",
                channel_id=-1001,
                username="Secret_Channel",
                title="Sensitive Title",
            ),
            "https://t.me/another_secret": _resolved(
                "https://t.me/another_secret",
                input_type="tme_url",
                channel_id=-1002,
                username="another_secret",
                title="Another Secret Title",
            ),
        }
    )

    report = await resolve_source_channels(watchlist, resolver)

    assert resolver.calls == [
        ("@Secret_Channel", "username"),
        ("https://t.me/another_secret", "tme_url"),
    ]
    assert report.resolved_count == 2
    assert report.allow_P6_2C == "yes"
    assert [item.numeric_channel_id for item in report.results] == [-1001, -1002]


@pytest.mark.asyncio
async def test_resolver_unavailable_blocks_username_refs() -> None:
    watchlist = WatchlistConfig(
        source_channels=[{"ref": "@Secret_Channel"}],
        watch_titles=[],
    )

    report = await resolve_source_channels(watchlist, resolver=None)

    assert report.resolver_unavailable_count == 1
    assert report.allow_P6_2C == "no"
    assert report.results[0].error_code == "SESSION_UNAVAILABLE"


@pytest.mark.asyncio
async def test_known_resolver_errors_are_mapped_to_stable_statuses() -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "@missing_user"},
            {"ref": "@private_channel"},
        ],
        watch_titles=[],
    )
    resolver = FakeChannelResolver(
        {
            "@missing_user": ChannelResolveError(
                "not_found",
                "USERNAME_NOT_FOUND",
            ),
            "@private_channel": ChannelResolveError(
                "private_or_forbidden",
                "CHANNEL_PRIVATE",
            ),
        }
    )

    report = await resolve_source_channels(watchlist, resolver)

    assert report.not_found_count == 1
    assert report.private_or_forbidden_count == 1
    assert report.resolver_error_count == 0
    assert report.allow_P6_2C == "no"
    assert [item.error_code for item in report.results] == [
        "USERNAME_NOT_FOUND",
        "CHANNEL_PRIVATE",
    ]


@pytest.mark.asyncio
async def test_unknown_resolver_exception_becomes_resolver_error() -> None:
    watchlist = WatchlistConfig(
        source_channels=[{"ref": "@boom_channel"}],
        watch_titles=[],
    )
    resolver = FakeChannelResolver(
        {"@boom_channel": RuntimeError("adapter internals must not leak")}
    )

    report = await resolve_source_channels(watchlist, resolver)

    assert report.resolver_error_count == 1
    assert report.results[0].error_code == "UNEXPECTED_EXCEPTION"
    assert report.allow_P6_2C == "no"


@pytest.mark.asyncio
async def test_invalid_resolver_result_is_downgraded_to_resolver_error() -> None:
    watchlist = WatchlistConfig(
        source_channels=[{"ref": "@bad_adapter"}],
        watch_titles=[],
    )
    resolver = FakeChannelResolver(
        {
            "@bad_adapter": ChannelResolveResult(
                input_ref="@bad_adapter",
                input_type="username",
                status="resolved",
                numeric_channel_id=None,
                username="bad_adapter",
            )
        }
    )

    report = await resolve_source_channels(watchlist, resolver)

    assert report.resolver_error_count == 1
    assert report.results[0].status == "resolver_error"
    assert report.results[0].error_code == "INVALID_RESOLVER_RESULT"
    assert report.allow_P6_2C == "no"


@pytest.mark.asyncio
async def test_duplicate_refs_are_counted_but_preserved() -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "@dup_channel"},
            {"ref": "@Dup_Channel"},
        ],
        watch_titles=[],
    )
    resolver = FakeChannelResolver(
        {
            "@dup_channel": _resolved("@dup_channel", channel_id=-1001),
            "@Dup_Channel": _resolved("@Dup_Channel", channel_id=-1002),
        }
    )

    report = await resolve_source_channels(watchlist, resolver)

    assert report.duplicate_ref_count == 1
    assert len(report.results) == 2
    assert report.resolved_count == 2


@pytest.mark.asyncio
async def test_report_is_desensitized_and_count_conservation_holds() -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "-1001234567890"},
            {"ref": "@Sensitive_Channel"},
            {"ref": "https://example.com/not-telegram"},
        ],
        watch_titles=[],
    )
    resolver = FakeChannelResolver(
        {
            "@Sensitive_Channel": _resolved(
                "@Sensitive_Channel",
                channel_id=-1009,
                username="Sensitive_Channel",
                title="Sensitive Title",
            )
        }
    )

    report = await resolve_source_channels(watchlist, resolver)
    serialized = json.dumps(report.model_dump(), ensure_ascii=False)

    assert "Sensitive_Channel" not in serialized
    assert "Sensitive Title" not in serialized
    assert "https://example.com/not-telegram" not in serialized
    assert report.result_count_matches_enabled_count is True
    counted = (
        report.already_numeric_count
        + report.resolved_count
        + report.not_found_count
        + report.private_or_forbidden_count
        + report.invalid_ref_count
        + report.resolver_error_count
        + report.resolver_unavailable_count
    )
    assert counted == report.enabled_source_channels
    assert report.telegram_api_accessed == "no"
    assert report.database_accessed == "no"
    assert report.listener_started == "no"
    assert report.handler_registered == "no"
    assert report.long_running_process == "no"
