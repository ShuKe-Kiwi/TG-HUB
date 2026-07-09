import json

from app.config import Settings
from app.modules.monitor.config import WatchlistConfig
from app.modules.monitor.runtime_preflight import (
    build_runtime_preflight_report,
    run_runtime_preflight,
)


def _settings(tmp_path, watchlist_path):
    return Settings(
        WATCHLIST_PATH=watchlist_path,
        TELETHON_API_ID=123456,
        TELETHON_API_HASH="secret-hash",
        TELETHON_SESSION_PATH=tmp_path / "telethon.session",
    )


def _write_watchlist(path, data) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_runtime_preflight_passes_for_ready_username_resolution(tmp_path) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    _write_watchlist(
        watchlist_path,
        {
            "source_channels": [
                {"ref": "@demo_channel"},
                {"ref": "https://t.me/another_demo"},
                {"ref": "-1001234567890"},
            ],
            "watch_titles": [{"title": "示例资源"}],
        },
    )
    session_path = tmp_path / "telethon.session"

    report = run_runtime_preflight(
        _settings(tmp_path, watchlist_path),
        telethon_probe=lambda: True,
        path_writable_probe=lambda path: path == tmp_path,
    )

    assert report.ready_for_one_shot_resolve == "yes"
    assert report.watchlist_schema == "pass"
    assert report.telethon_dependency == "pass"
    assert report.enabled_source_channels == 3
    assert report.numeric_source_channels == 1
    assert report.resolver_required_source_channels == 2
    assert report.invalid_source_channels == 0
    assert report.blockers == []
    assert report.telegram_api_accessed == "no"
    assert report.client_created == "no"
    assert report.client_connected == "no"
    assert report.handler_registered == "no"
    assert report.database_accessed == "no"
    assert report.session_file_written == "no"
    assert session_path.exists() is False


def test_runtime_preflight_reports_missing_runtime_requirements(tmp_path) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    _write_watchlist(
        watchlist_path,
        {
            "source_channels": [{"ref": "@demo_channel"}],
            "watch_titles": [{"title": "示例资源"}],
        },
    )
    missing_parent = tmp_path / "missing" / "telethon.session"
    settings = Settings(
        WATCHLIST_PATH=watchlist_path,
        TELETHON_API_ID=None,
        TELETHON_API_HASH="",
        TELETHON_SESSION_PATH=missing_parent,
    )

    report = run_runtime_preflight(
        settings,
        telethon_probe=lambda: False,
        path_writable_probe=lambda path: True,
    )

    assert report.ready_for_one_shot_resolve == "no"
    assert report.telethon_dependency == "fail"
    assert report.telethon_api_id_configured == "fail"
    assert report.telethon_api_hash_configured == "fail"
    assert report.telethon_session_parent_exists == "fail"
    assert report.blockers == [
        "telethon_dependency_missing",
        "telethon_api_id_missing",
        "telethon_api_hash_missing",
        "telethon_session_parent_missing",
    ]


def test_runtime_preflight_does_not_require_telethon_for_numeric_only_refs(
    tmp_path,
) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    _write_watchlist(
        watchlist_path,
        {
            "source_channels": [{"ref": "-1001234567890"}],
            "watch_titles": [],
        },
    )
    settings = Settings(
        WATCHLIST_PATH=watchlist_path,
        TELETHON_API_ID=None,
        TELETHON_API_HASH="",
        TELETHON_SESSION_PATH=tmp_path / "missing" / "telethon.session",
    )

    report = run_runtime_preflight(
        settings,
        telethon_probe=lambda: False,
        path_writable_probe=lambda path: False,
    )

    assert report.ready_for_one_shot_resolve == "yes"
    assert report.numeric_source_channels == 1
    assert report.resolver_required_source_channels == 0
    assert report.blockers == []


def test_runtime_preflight_blocks_invalid_or_empty_sources(tmp_path) -> None:
    watchlist = WatchlistConfig(
        source_channels=[
            {"ref": "https://example.com/not-telegram"},
            {"ref": "@disabled_channel", "enabled": False},
        ],
        watch_titles=[],
    )

    report = build_runtime_preflight_report(
        watchlist,
        app_settings=_settings(tmp_path, tmp_path / "unused.json"),
        telethon_probe=lambda: True,
        path_writable_probe=lambda path: True,
    )

    assert report.ready_for_one_shot_resolve == "no"
    assert report.enabled_source_channels == 1
    assert report.invalid_source_channels == 1
    assert report.blockers == ["invalid_source_channels"]


def test_runtime_preflight_reports_unreadable_watchlist(tmp_path) -> None:
    report = run_runtime_preflight(
        _settings(tmp_path, tmp_path / "missing.json"),
        telethon_probe=lambda: True,
        path_writable_probe=lambda path: True,
    )

    assert report.watchlist_readable == "fail"
    assert report.watchlist_schema == "fail"
    assert report.ready_for_one_shot_resolve == "no"
    assert report.blockers == ["watchlist_unreadable_or_invalid"]
