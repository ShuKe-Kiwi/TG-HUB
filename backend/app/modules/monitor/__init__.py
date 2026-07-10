"""Monitor configuration and watchlist helpers."""

from app.modules.monitor.config import (
    SourceChannelConfig,
    WatchTitleConfig,
    WatchlistConfig,
    load_watchlist,
)
from app.modules.monitor.channel_ids import (
    CanonicalChannelIdResult,
    canonicalize_source_channel_id,
)
from app.modules.monitor.filter import (
    WatchlistMatchResult,
    filter_message,
    normalize_watch_text,
)
from app.modules.monitor.listener_dry_run import (
    IncomingMessageAdapter,
    MonitorDryRunEventReport,
    MonitorDryRunReport,
    TelethonIncomingMessageAdapter,
    TelegramClientLike,
    create_telethon_client_from_settings,
    run_monitor_dry_run,
)
from app.modules.monitor.schema import (
    IncomingMessage,
)
from app.modules.monitor.resolver import (
    ChannelResolveError,
    ChannelResolveResult,
    ChannelResolver,
    ResolvedSourceChannelReport,
    ResolvedSourceChannelReportItem,
    resolve_source_channels,
)
from app.modules.monitor.source_channels import (
    SourceChannelPrecheckResult,
    precheck_source_channels,
)
from app.modules.monitor.runtime_preflight import (
    RuntimePreflightReport,
    build_runtime_preflight_report,
    run_runtime_preflight,
)
from app.modules.monitor.runtime import (
    IncomingMessageIngestionBoundary,
    MonitorHealth,
    MonitorHeartbeat,
    MonitorRuntime,
    MonitorRuntimeConfig,
    MonitorRuntimeError,
    MonitorRuntimeSummary,
    run_monitor_runtime_from_settings,
)
from app.modules.monitor.telethon_resolver import (
    TelethonControlledChannelResolver,
    resolve_watchlist_once,
)

__all__ = [
    "ChannelResolveError",
    "ChannelResolveResult",
    "ChannelResolver",
    "CanonicalChannelIdResult",
    "IncomingMessageAdapter",
    "IncomingMessageIngestionBoundary",
    "IncomingMessage",
    "MonitorDryRunEventReport",
    "MonitorDryRunReport",
    "MonitorHealth",
    "MonitorHeartbeat",
    "MonitorRuntime",
    "MonitorRuntimeConfig",
    "MonitorRuntimeError",
    "MonitorRuntimeSummary",
    "RuntimePreflightReport",
    "ResolvedSourceChannelReport",
    "ResolvedSourceChannelReportItem",
    "SourceChannelConfig",
    "SourceChannelPrecheckResult",
    "TelegramClientLike",
    "TelethonIncomingMessageAdapter",
    "TelethonControlledChannelResolver",
    "WatchTitleConfig",
    "WatchlistConfig",
    "WatchlistMatchResult",
    "build_runtime_preflight_report",
    "canonicalize_source_channel_id",
    "create_telethon_client_from_settings",
    "filter_message",
    "load_watchlist",
    "normalize_watch_text",
    "precheck_source_channels",
    "resolve_source_channels",
    "resolve_watchlist_once",
    "run_monitor_dry_run",
    "run_monitor_runtime_from_settings",
    "run_runtime_preflight",
]
