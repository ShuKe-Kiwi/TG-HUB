"""Pure source channel parsing precheck.

P6-2C-0 does not resolve usernames through Telegram. It only classifies
configured refs and extracts numeric IDs when the ref is already numeric.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from app.modules.monitor.config import WatchlistConfig

SourceChannelInputType = Literal[
    "numeric_id",
    "username",
    "tme_url",
    "invalid",
]
SourceChannelPrecheckStatus = Literal[
    "parsed_numeric_id",
    "username_requires_resolution",
    "invalid_ref",
]

_NUMERIC_ID_RE = re.compile(r"-?\d+")
_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")


class SourceChannelPrecheckResult(BaseModel):
    """Desensitized result for one enabled source channel config."""

    model_config = ConfigDict(frozen=True)

    index: int
    input_type: SourceChannelInputType
    status: SourceChannelPrecheckStatus
    numeric_channel_id: int | None = None
    masked_ref: str
    masked_username: str | None = None


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
    if _NUMERIC_ID_RE.fullmatch(ref):
        return _mask_value(ref)
    return _mask_value(ref)


def _username_from_tme_url(ref: str) -> str | None:
    parsed = urlparse(ref)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.netloc.casefold() != "t.me":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 1:
        return None

    username = parts[0]
    if _USERNAME_RE.fullmatch(username):
        return username
    return None


def _precheck_one(ref: str, index: int) -> SourceChannelPrecheckResult:
    normalized_ref = ref.strip()
    if _NUMERIC_ID_RE.fullmatch(normalized_ref):
        return SourceChannelPrecheckResult(
            index=index,
            input_type="numeric_id",
            status="parsed_numeric_id",
            numeric_channel_id=int(normalized_ref),
            masked_ref=_mask_ref(normalized_ref),
        )

    if normalized_ref.startswith("@"):
        username = normalized_ref[1:]
        if _USERNAME_RE.fullmatch(username):
            return SourceChannelPrecheckResult(
                index=index,
                input_type="username",
                status="username_requires_resolution",
                masked_ref=_mask_ref(normalized_ref),
                masked_username=_mask_value(username),
            )

    username = _username_from_tme_url(normalized_ref)
    if username is not None:
        return SourceChannelPrecheckResult(
            index=index,
            input_type="tme_url",
            status="username_requires_resolution",
            masked_ref=_mask_ref(normalized_ref),
            masked_username=_mask_value(username),
        )

    return SourceChannelPrecheckResult(
        index=index,
        input_type="invalid",
        status="invalid_ref",
        masked_ref=_mask_ref(normalized_ref),
    )


def precheck_source_channels(
    watchlist: WatchlistConfig,
) -> list[SourceChannelPrecheckResult]:
    """Precheck enabled source_channels without resolving Telegram entities."""
    results: list[SourceChannelPrecheckResult] = []
    for index, channel in enumerate(watchlist.source_channels):
        if not channel.enabled:
            continue
        results.append(_precheck_one(channel.ref, index=index))
    return results
