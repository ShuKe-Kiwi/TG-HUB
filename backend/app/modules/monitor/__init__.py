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

__all__ = [
    "IncomingMessage",
    "SourceChannelConfig",
    "WatchTitleConfig",
    "WatchlistConfig",
    "WatchlistMatchResult",
    "filter_message",
    "load_watchlist",
    "normalize_watch_text",
]
