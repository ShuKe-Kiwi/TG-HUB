"""Controlled source channel resolution orchestration.

P6-2C-0B-1 deliberately uses a resolver protocol and fake test doubles only.
It does not import Telethon, start listeners, access Telegram, or persist data.
"""

from __future__ import annotations

from typing import Literal, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.source_channels import precheck_source_channels

ResolvableInputType = Literal["username", "tme_url"]
ReportInputType = Literal["numeric_id", "username", "tme_url", "invalid"]
ResolveStatus = Literal[
    "resolved",
    "already_numeric",
    "not_found",
    "private_or_forbidden",
    "invalid_ref",
    "resolver_unavailable",
    "resolver_error",
]
ResolveErrorCode = Literal[
    "USERNAME_NOT_FOUND",
    "CHANNEL_PRIVATE",
    "ACCESS_FORBIDDEN",
    "SESSION_UNAVAILABLE",
    "CLIENT_NOT_CONNECTED",
    "FLOOD_WAIT",
    "RPC_ERROR",
    "UNEXPECTED_EXCEPTION",
    "INVALID_RESOLVER_RESULT",
    "INVALID_REF",
]


class ChannelResolveResult(BaseModel):
    """Internal resolver result. Do not serialize this as a public report."""

    model_config = ConfigDict(frozen=True)

    input_ref: str
    input_type: ResolvableInputType
    status: ResolveStatus
    numeric_channel_id: int | None = None
    username: str | None = None
    title: str | None = None
    error_code: ResolveErrorCode | None = None


class ChannelResolveError(Exception):
    """Stable resolver exception wrapper for adapter-specific failures."""

    def __init__(
        self,
        status: ResolveStatus,
        error_code: ResolveErrorCode,
    ) -> None:
        super().__init__(error_code)
        self.status = status
        self.error_code = error_code


class ChannelResolver(Protocol):
    async def resolve(
        self,
        ref: str,
        *,
        input_type: ResolvableInputType,
    ) -> ChannelResolveResult:
        """Resolve a username-like channel ref to a numeric channel id."""


class ResolvedSourceChannelReportItem(BaseModel):
    """Desensitized report item for one enabled source channel."""

    model_config = ConfigDict(frozen=True)

    index: int
    input_type: ReportInputType
    status: ResolveStatus
    numeric_channel_id: int | None
    masked_ref: str
    masked_username: str | None = None
    masked_title: str | None = None
    error_code: ResolveErrorCode | None = None


class ResolvedSourceChannelReport(BaseModel):
    """Summary report for a controlled one-shot source channel resolution."""

    model_config = ConfigDict(frozen=True)

    watchlist_schema: Literal["pass"] = "pass"
    enabled_source_channels: int
    already_numeric_count: int
    resolved_count: int
    not_found_count: int
    private_or_forbidden_count: int
    invalid_ref_count: int
    resolver_error_count: int
    resolver_unavailable_count: int
    duplicate_ref_count: int
    result_count_matches_enabled_count: bool
    telegram_api_accessed: Literal["no"] = "no"
    database_accessed: Literal["no"] = "no"
    parser_called: Literal["no"] = "no"
    normalizer_called: Literal["no"] = "no"
    dedup_called: Literal["no"] = "no"
    notification_sent: Literal["no"] = "no"
    media_downloaded: Literal["no"] = "no"
    listener_started: Literal["no"] = "no"
    handler_registered: Literal["no"] = "no"
    long_running_process: Literal["no"] = "no"
    report_desensitized: Literal["yes"] = "yes"
    allow_P6_2C: Literal["yes", "no"]
    blockers: list[str]
    results: list[ResolvedSourceChannelReportItem]


def _mask_value(value: str) -> str:
    if len(value) <= 2:
        return "*" * len(value)
    if len(value) <= 6:
        return f"{value[0]}***{value[-1]}"
    return f"{value[:2]}***{value[-2:]}"


def _mask_ref(ref: str) -> str:
    parsed = urlparse(ref)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}/***"
    if ref.startswith("@"):
        return f"@{_mask_value(ref[1:])}"
    return _mask_value(ref)


def _mask_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return _mask_value(stripped)


def _report_item_from_resolver_result(
    *,
    index: int,
    source_ref: str,
    input_type: ResolvableInputType,
    result: ChannelResolveResult,
) -> ResolvedSourceChannelReportItem:
    status = result.status
    numeric_channel_id = result.numeric_channel_id
    error_code = result.error_code

    if status == "resolved" and numeric_channel_id is None:
        status = "resolver_error"
        error_code = "INVALID_RESOLVER_RESULT"

    return ResolvedSourceChannelReportItem(
        index=index,
        input_type=input_type,
        status=status,
        numeric_channel_id=numeric_channel_id if status == "resolved" else None,
        masked_ref=_mask_ref(source_ref),
        masked_username=_mask_optional(result.username),
        masked_title=_mask_optional(result.title),
        error_code=error_code,
    )


def _report_item_for_exception(
    *,
    index: int,
    source_ref: str,
    input_type: ResolvableInputType,
    exc: Exception,
) -> ResolvedSourceChannelReportItem:
    if isinstance(exc, ChannelResolveError):
        return ResolvedSourceChannelReportItem(
            index=index,
            input_type=input_type,
            status=exc.status,
            numeric_channel_id=None,
            masked_ref=_mask_ref(source_ref),
            error_code=exc.error_code,
        )

    return ResolvedSourceChannelReportItem(
        index=index,
        input_type=input_type,
        status="resolver_error",
        numeric_channel_id=None,
        masked_ref=_mask_ref(source_ref),
        error_code="UNEXPECTED_EXCEPTION",
    )


def _count_duplicates(refs: list[str]) -> int:
    seen: set[str] = set()
    duplicate_count = 0
    for ref in refs:
        normalized = ref.strip().casefold()
        if normalized in seen:
            duplicate_count += 1
        else:
            seen.add(normalized)
    return duplicate_count


def _build_report(
    *,
    results: list[ResolvedSourceChannelReportItem],
    duplicate_ref_count: int,
) -> ResolvedSourceChannelReport:
    counts = {
        "already_numeric_count": 0,
        "resolved_count": 0,
        "not_found_count": 0,
        "private_or_forbidden_count": 0,
        "invalid_ref_count": 0,
        "resolver_error_count": 0,
        "resolver_unavailable_count": 0,
    }
    for result in results:
        if result.status == "already_numeric":
            counts["already_numeric_count"] += 1
        elif result.status == "resolved":
            counts["resolved_count"] += 1
        elif result.status == "not_found":
            counts["not_found_count"] += 1
        elif result.status == "private_or_forbidden":
            counts["private_or_forbidden_count"] += 1
        elif result.status == "invalid_ref":
            counts["invalid_ref_count"] += 1
        elif result.status == "resolver_error":
            counts["resolver_error_count"] += 1
        elif result.status == "resolver_unavailable":
            counts["resolver_unavailable_count"] += 1

    enabled_count = len(results)
    counted_total = sum(counts.values())
    result_count_matches_enabled_count = counted_total == enabled_count
    blockers: list[str] = []
    if not result_count_matches_enabled_count:
        blockers.append("result_count_mismatch")
    for key, value in counts.items():
        if key != "already_numeric_count" and key != "resolved_count" and value > 0:
            blockers.append(key)

    allow_p6_2c = "yes" if not blockers else "no"
    return ResolvedSourceChannelReport(
        enabled_source_channels=enabled_count,
        duplicate_ref_count=duplicate_ref_count,
        result_count_matches_enabled_count=result_count_matches_enabled_count,
        allow_P6_2C=allow_p6_2c,
        blockers=blockers,
        results=results,
        **counts,
    )


async def resolve_source_channels(
    watchlist: WatchlistConfig,
    resolver: ChannelResolver | None,
) -> ResolvedSourceChannelReport:
    """Resolve enabled source channels in a controlled one-shot flow."""
    precheck_results = precheck_source_channels(watchlist)
    enabled_refs = [
        channel.ref for channel in watchlist.source_channels if channel.enabled
    ]
    duplicate_ref_count = _count_duplicates(enabled_refs)

    report_items: list[ResolvedSourceChannelReportItem] = []
    for precheck in precheck_results:
        source_ref = watchlist.source_channels[precheck.index].ref.strip()
        if precheck.input_type == "numeric_id":
            report_items.append(
                ResolvedSourceChannelReportItem(
                    index=precheck.index,
                    input_type="numeric_id",
                    status="already_numeric",
                    numeric_channel_id=precheck.numeric_channel_id,
                    masked_ref=precheck.masked_ref,
                )
            )
            continue

        if precheck.input_type == "invalid":
            report_items.append(
                ResolvedSourceChannelReportItem(
                    index=precheck.index,
                    input_type="invalid",
                    status="invalid_ref",
                    numeric_channel_id=None,
                    masked_ref=precheck.masked_ref,
                    error_code="INVALID_REF",
                )
            )
            continue

        if resolver is None:
            report_items.append(
                ResolvedSourceChannelReportItem(
                    index=precheck.index,
                    input_type=precheck.input_type,
                    status="resolver_unavailable",
                    numeric_channel_id=None,
                    masked_ref=precheck.masked_ref,
                    masked_username=precheck.masked_username,
                    error_code="SESSION_UNAVAILABLE",
                )
            )
            continue

        try:
            resolved = await resolver.resolve(
                source_ref,
                input_type=precheck.input_type,
            )
        except Exception as exc:
            report_items.append(
                _report_item_for_exception(
                    index=precheck.index,
                    source_ref=source_ref,
                    input_type=precheck.input_type,
                    exc=exc,
                )
            )
            continue

        report_items.append(
            _report_item_from_resolver_result(
                index=precheck.index,
                source_ref=source_ref,
                input_type=precheck.input_type,
                result=resolved,
            )
        )

    return _build_report(
        results=report_items,
        duplicate_ref_count=duplicate_ref_count,
    )
