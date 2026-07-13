"""Purely local startup preflight for the monitor CLI."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from app.config import Settings, settings
from app.modules.monitor.config import WatchlistConfig, load_watchlist
from app.modules.monitor.session_ownership import (
    SessionOwnershipError,
    validate_session_file,
)
from app.modules.monitor.source_channels import precheck_source_channels

Status = Literal["pass", "fail"]
Flag = Literal["yes", "no"]


class MonitorStartupPreflightReport(BaseModel):
    """Desensitized report containing static and local checks only."""

    model_config = ConfigDict(frozen=True)

    status: Status
    watchlist_loaded: Flag
    watchlist_schema: Status
    telethon_dependency: Status
    telegram_api_id_configured: Status
    telegram_api_hash_configured: Status
    session_configured: Status
    session_parent_exists: Status
    session_parent_writable: Status
    session_file_exists: Status = "pass"
    session_file_regular: Status = "pass"
    session_file_permissions: Status = "pass"
    database_url_configured: Status
    enabled_source_channels: int
    invalid_source_channels: int
    enabled_watch_titles: int
    telegram_api_accessed: Literal["no"] = "no"
    database_accessed: Literal["no"] = "no"
    bot_api_accessed: Literal["no"] = "no"
    report_desensitized: Literal["yes"] = "yes"
    blockers: list[str]


class StaticStartupPreflight:
    """Validate local startup prerequisites without external I/O."""

    def __init__(
        self,
        app_settings: Settings | None = None,
        *,
        dependency_probe: Callable[[], bool] | None = None,
        writable_probe: Callable[[Path], bool] | None = None,
    ) -> None:
        self.settings = app_settings or settings
        self._dependency_probe = dependency_probe or (
            lambda: importlib.util.find_spec("telethon") is not None
        )
        self._writable_probe = writable_probe or (
            lambda path: os.access(path, os.W_OK)
        )

    def run(
        self,
        watchlist_snapshot: WatchlistConfig | None = None,
    ) -> MonitorStartupPreflightReport:
        blockers: list[str] = []
        enabled_sources = 0
        invalid_sources = 0
        enabled_titles = 0
        watchlist_loaded = False
        watchlist_valid = False

        try:
            watchlist = watchlist_snapshot or load_watchlist(
                self.settings.WATCHLIST_PATH
            )
            watchlist_loaded = True
            watchlist_valid = True
            source_results = precheck_source_channels(watchlist)
            enabled_sources = len(source_results)
            invalid_sources = sum(
                item.status == "invalid_ref" for item in source_results
            )
            enabled_titles = len(watchlist.enabled_watch_titles())
        except FileNotFoundError:
            blockers.append("watchlist_not_found")
        except Exception:
            blockers.append("watchlist_unreadable_or_invalid")

        telethon_ready = self._dependency_probe()
        api_id_ready = self.settings.TELEGRAM_API_ID is not None
        api_hash_ready = bool(self.settings.TELEGRAM_API_HASH.strip())
        session_ready = bool(self.settings.TELEGRAM_SESSION_NAME.strip())
        session_parent = Path(
            self.settings.TELEGRAM_SESSION_NAME or "."
        ).expanduser().parent
        parent_exists = session_parent.exists()
        parent_writable = (
            self._writable_probe(session_parent) if parent_exists else False
        )
        database_ready = bool(self.settings.DATABASE_URL.strip())
        session_file_ready = False
        try:
            validate_session_file(self.settings)
            session_file_ready = True
        except SessionOwnershipError:
            pass

        checks = (
            (telethon_ready, "telethon_dependency_missing"),
            (api_id_ready, "telegram_api_id_missing"),
            (api_hash_ready, "telegram_api_hash_missing"),
            (session_ready, "telegram_session_name_missing"),
            (parent_exists, "telegram_session_parent_missing"),
            (parent_writable, "telegram_session_parent_not_writable"),
            (session_file_ready, "telegram_session_file_invalid"),
            (database_ready, "database_url_missing"),
            (enabled_sources > 0, "no_enabled_source_channels"),
            (invalid_sources == 0, "invalid_source_channels"),
            (enabled_titles > 0, "no_enabled_watch_titles"),
        )
        for passed, blocker in checks:
            if not passed and blocker not in blockers:
                blockers.append(blocker)

        return MonitorStartupPreflightReport(
            status="pass" if not blockers else "fail",
            watchlist_loaded="yes" if watchlist_loaded else "no",
            watchlist_schema="pass" if watchlist_valid else "fail",
            telethon_dependency="pass" if telethon_ready else "fail",
            telegram_api_id_configured="pass" if api_id_ready else "fail",
            telegram_api_hash_configured="pass" if api_hash_ready else "fail",
            session_configured="pass" if session_ready else "fail",
            session_parent_exists="pass" if parent_exists else "fail",
            session_parent_writable="pass" if parent_writable else "fail",
            session_file_exists="pass" if session_file_ready else "fail",
            session_file_regular="pass" if session_file_ready else "fail",
            session_file_permissions="pass" if session_file_ready else "fail",
            database_url_configured="pass" if database_ready else "fail",
            enabled_source_channels=enabled_sources,
            invalid_source_channels=invalid_sources,
            enabled_watch_titles=enabled_titles,
            blockers=blockers,
        )


def run_static_startup_preflight(
    app_settings: Settings | None = None,
) -> MonitorStartupPreflightReport:
    return StaticStartupPreflight(app_settings).run()


def build_static_startup_preflight(
    watchlist_snapshot: WatchlistConfig,
    app_settings: Settings | None = None,
) -> MonitorStartupPreflightReport:
    """Build static checks from the same frozen watchlist used online."""
    return StaticStartupPreflight(app_settings).run(watchlist_snapshot)
