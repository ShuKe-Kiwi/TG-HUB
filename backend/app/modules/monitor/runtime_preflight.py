"""Runtime readiness preflight for one-shot Telethon channel resolution.

This module checks local runtime prerequisites only. It must not create a
Telethon client, connect to Telegram, register handlers, or persist data.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.config import Settings, settings
from app.modules.monitor.config import WatchlistConfig, load_watchlist
from app.modules.monitor.source_channels import precheck_source_channels

PreflightStatus = Literal["pass", "fail"]
PreflightFlag = Literal["yes", "no"]

TelethonProbe = Callable[[], bool]
PathWritableProbe = Callable[[Path], bool]


class RuntimePreflightReport(BaseModel):
    """Desensitized runtime readiness report for P6-2C-0B-2."""

    model_config = ConfigDict(frozen=True)

    watchlist_schema: PreflightStatus
    watchlist_readable: PreflightStatus
    telethon_dependency: PreflightStatus
    telethon_api_id_configured: PreflightStatus
    telethon_api_hash_configured: PreflightStatus
    telethon_session_parent_exists: PreflightStatus
    telethon_session_parent_writable: PreflightStatus
    enabled_source_channels: int
    numeric_source_channels: int
    resolver_required_source_channels: int
    invalid_source_channels: int
    watch_title_entries: int
    ready_for_one_shot_resolve: PreflightFlag
    telegram_api_accessed: Literal["no"] = "no"
    client_created: Literal["no"] = "no"
    client_connected: Literal["no"] = "no"
    database_accessed: Literal["no"] = "no"
    parser_called: Literal["no"] = "no"
    normalizer_called: Literal["no"] = "no"
    dedup_called: Literal["no"] = "no"
    notification_sent: Literal["no"] = "no"
    media_downloaded: Literal["no"] = "no"
    listener_started: Literal["no"] = "no"
    handler_registered: Literal["no"] = "no"
    long_running_process: Literal["no"] = "no"
    session_file_written: Literal["no"] = "no"
    report_desensitized: Literal["yes"] = "yes"
    blockers: list[str]


def _status(value: bool) -> PreflightStatus:
    return "pass" if value else "fail"


def _probe_telethon_importable() -> bool:
    return importlib.util.find_spec("telethon") is not None


def _probe_path_writable(path: Path) -> bool:
    return os.access(path, os.W_OK)


def _failed_watchlist_report(
    *,
    app_settings: Settings,
    telethon_probe: TelethonProbe,
    path_writable_probe: PathWritableProbe,
    blockers: list[str],
) -> RuntimePreflightReport:
    session_parent = app_settings.TELETHON_SESSION_PATH.expanduser().parent
    telethon_dependency = telethon_probe()
    api_id_configured = app_settings.TELETHON_API_ID is not None
    api_hash_configured = bool(app_settings.TELETHON_API_HASH.strip())
    session_parent_exists = session_parent.exists()
    session_parent_writable = (
        path_writable_probe(session_parent) if session_parent_exists else False
    )

    if not telethon_dependency:
        blockers.append("telethon_dependency_missing")
    if not api_id_configured:
        blockers.append("telethon_api_id_missing")
    if not api_hash_configured:
        blockers.append("telethon_api_hash_missing")
    if not session_parent_exists:
        blockers.append("telethon_session_parent_missing")
    elif not session_parent_writable:
        blockers.append("telethon_session_parent_not_writable")

    return RuntimePreflightReport(
        watchlist_schema="fail",
        watchlist_readable="fail",
        telethon_dependency=_status(telethon_dependency),
        telethon_api_id_configured=_status(api_id_configured),
        telethon_api_hash_configured=_status(api_hash_configured),
        telethon_session_parent_exists=_status(session_parent_exists),
        telethon_session_parent_writable=_status(session_parent_writable),
        enabled_source_channels=0,
        numeric_source_channels=0,
        resolver_required_source_channels=0,
        invalid_source_channels=0,
        watch_title_entries=0,
        ready_for_one_shot_resolve="no",
        blockers=blockers,
    )


def run_runtime_preflight(
    app_settings: Settings | None = None,
    *,
    telethon_probe: TelethonProbe | None = None,
    path_writable_probe: PathWritableProbe | None = None,
) -> RuntimePreflightReport:
    """Check local readiness for controlled one-shot source channel resolve."""
    resolved_settings = app_settings or settings
    resolved_telethon_probe = telethon_probe or _probe_telethon_importable
    resolved_path_writable_probe = path_writable_probe or _probe_path_writable
    blockers: list[str] = []

    try:
        watchlist = load_watchlist(resolved_settings.WATCHLIST_PATH)
        watchlist_readable = True
        watchlist_schema = True
    except Exception:
        blockers.append("watchlist_unreadable_or_invalid")
        return _failed_watchlist_report(
            app_settings=resolved_settings,
            telethon_probe=resolved_telethon_probe,
            path_writable_probe=resolved_path_writable_probe,
            blockers=blockers,
        )

    return build_runtime_preflight_report(
        watchlist,
        app_settings=resolved_settings,
        telethon_probe=resolved_telethon_probe,
        path_writable_probe=resolved_path_writable_probe,
        watchlist_readable=watchlist_readable,
        watchlist_schema=watchlist_schema,
        blockers=blockers,
    )


def build_runtime_preflight_report(
    watchlist: WatchlistConfig,
    *,
    app_settings: Settings | None = None,
    telethon_probe: TelethonProbe | None = None,
    path_writable_probe: PathWritableProbe | None = None,
    watchlist_readable: bool = True,
    watchlist_schema: bool = True,
    blockers: list[str] | None = None,
) -> RuntimePreflightReport:
    """Build a preflight report from an already validated watchlist."""
    resolved_settings = app_settings or settings
    resolved_telethon_probe = telethon_probe or _probe_telethon_importable
    resolved_path_writable_probe = path_writable_probe or _probe_path_writable
    resolved_blockers = list(blockers or [])

    source_results = precheck_source_channels(watchlist)
    enabled_source_channels = len(source_results)
    numeric_source_channels = sum(
        result.status == "parsed_numeric_id" for result in source_results
    )
    resolver_required_source_channels = sum(
        result.status == "username_requires_resolution"
        for result in source_results
    )
    invalid_source_channels = sum(
        result.status == "invalid_ref" for result in source_results
    )

    telethon_dependency = resolved_telethon_probe()
    api_id_configured = resolved_settings.TELETHON_API_ID is not None
    api_hash_configured = bool(resolved_settings.TELETHON_API_HASH.strip())
    session_parent = resolved_settings.TELETHON_SESSION_PATH.expanduser().parent
    session_parent_exists = session_parent.exists()
    session_parent_writable = (
        resolved_path_writable_probe(session_parent)
        if session_parent_exists
        else False
    )

    if not watchlist_readable or not watchlist_schema:
        resolved_blockers.append("watchlist_unreadable_or_invalid")
    if enabled_source_channels == 0:
        resolved_blockers.append("no_enabled_source_channels")
    if invalid_source_channels > 0:
        resolved_blockers.append("invalid_source_channels")
    if resolver_required_source_channels > 0:
        if not telethon_dependency:
            resolved_blockers.append("telethon_dependency_missing")
        if not api_id_configured:
            resolved_blockers.append("telethon_api_id_missing")
        if not api_hash_configured:
            resolved_blockers.append("telethon_api_hash_missing")
        if not session_parent_exists:
            resolved_blockers.append("telethon_session_parent_missing")
        elif not session_parent_writable:
            resolved_blockers.append("telethon_session_parent_not_writable")

    ready = "yes" if not resolved_blockers else "no"
    return RuntimePreflightReport(
        watchlist_schema=_status(watchlist_schema),
        watchlist_readable=_status(watchlist_readable),
        telethon_dependency=_status(telethon_dependency),
        telethon_api_id_configured=_status(api_id_configured),
        telethon_api_hash_configured=_status(api_hash_configured),
        telethon_session_parent_exists=_status(session_parent_exists),
        telethon_session_parent_writable=_status(session_parent_writable),
        enabled_source_channels=enabled_source_channels,
        numeric_source_channels=numeric_source_channels,
        resolver_required_source_channels=resolver_required_source_channels,
        invalid_source_channels=invalid_source_channels,
        watch_title_entries=len(watchlist.watch_titles),
        ready_for_one_shot_resolve=ready,
        blockers=resolved_blockers,
    )
