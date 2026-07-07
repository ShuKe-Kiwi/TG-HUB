"""Pure watchlist filtering for monitor input messages."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.schema import IncomingMessage

WatchlistMatchReason = Literal[
    "matched",
    "empty_content",
    "no_watch_titles",
    "no_title_match",
]

_BRACKET_TRANSLATION = str.maketrans(
    {
        "【": " ",
        "】": " ",
        "（": " ",
        "）": " ",
        "(": " ",
        ")": " ",
        "[": " ",
        "]": " ",
        "〔": " ",
        "〕": " ",
        "「": " ",
        "」": " ",
        "『": " ",
        "』": " ",
        "《": " ",
        "》": " ",
    }
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}年?", re.IGNORECASE)
_QUALITY_RE = re.compile(r"\b(?:4k|1080p|2160p)\b", re.IGNORECASE)
_FULL_EPISODE_RE = re.compile(r"全\s*\d+\s*集|全集")
_SINGLE_EPISODE_RE = re.compile(r"第\s*\d+\s*集")
_WHITESPACE_RE = re.compile(r"\s+")


class WatchlistMatchResult(BaseModel):
    """Result of filtering one IncomingMessage against watch titles."""

    model_config = ConfigDict(frozen=True)

    matched: bool
    matched_titles: list[str]
    reason: WatchlistMatchReason


def normalize_watch_text(value: str) -> str:
    """Normalize only approved P6-2B title matching noise."""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = normalized.translate(_BRACKET_TRANSLATION)
    normalized = _YEAR_RE.sub(" ", normalized)
    normalized = _QUALITY_RE.sub(" ", normalized)
    normalized = _FULL_EPISODE_RE.sub(" ", normalized)
    normalized = _SINGLE_EPISODE_RE.sub(" ", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized)
    return normalized.strip()


def filter_message(
    message: IncomingMessage,
    watchlist: WatchlistConfig,
) -> WatchlistMatchResult:
    """Filter one message by watch_titles only.

    P6-2B intentionally does not inspect source_channels.
    """
    content_norm = normalize_watch_text(message.content_text)
    if not content_norm:
        return WatchlistMatchResult(
            matched=False,
            matched_titles=[],
            reason="empty_content",
        )

    watched_titles = watchlist.enabled_watch_titles()
    if not watched_titles:
        return WatchlistMatchResult(
            matched=False,
            matched_titles=[],
            reason="no_watch_titles",
        )

    matched_titles: list[str] = []
    for watched in watched_titles:
        title_norm = normalize_watch_text(watched.title)
        if title_norm and title_norm in content_norm:
            matched_titles.append(watched.title)

    if matched_titles:
        return WatchlistMatchResult(
            matched=True,
            matched_titles=matched_titles,
            reason="matched",
        )

    return WatchlistMatchResult(
        matched=False,
        matched_titles=[],
        reason="no_title_match",
    )
