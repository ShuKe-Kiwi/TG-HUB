"""Monitor configuration and watchlist helpers."""

from app.modules.monitor.config import (
    SourceChannelConfig,
    WatchTitleConfig,
    WatchlistConfig,
    load_watchlist,
)
from app.modules.monitor.filter import (
    WatchlistMatchResult,
    filter_message,
    normalize_watch_text,
)
from app.modules.monitor.schema import (
    IncomingMessage,
)
from app.modules.monitor.source_channels import (
    SourceChannelPrecheckResult,
    precheck_source_channels,
)

__all__ = [
    "IncomingMessage",
    "SourceChannelConfig",
    "SourceChannelPrecheckResult",
    "WatchTitleConfig",
    "WatchlistConfig",
    "WatchlistMatchResult",
    "filter_message",
    "load_watchlist",
    "normalize_watch_text",
    "precheck_source_channels",
]
