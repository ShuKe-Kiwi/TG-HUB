import json
from pathlib import Path

from app.config import Settings
from app.modules.monitor.preflight import StaticStartupPreflight


def _write_watchlist(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "source_channels": [{"ref": "-1001234567890", "enabled": True}],
                "watch_titles": [{"title": "家业", "enabled": True}],
            }
        ),
        encoding="utf-8",
    )


def test_static_preflight_passes_without_external_access(tmp_path: Path) -> None:
    watchlist_path = tmp_path / "watchlist.json"
    _write_watchlist(watchlist_path)
    configured = Settings(
        WATCHLIST_PATH=watchlist_path,
        TELEGRAM_API_ID=123,
        TELEGRAM_API_HASH="secret-hash",
        TELEGRAM_SESSION_NAME=str(tmp_path / "session"),
        DATABASE_URL="postgresql+asyncpg://secret/db",
    )

    report = StaticStartupPreflight(
        configured,
        dependency_probe=lambda: True,
        writable_probe=lambda path: True,
    ).run()

    assert report.status == "pass"
    assert report.telegram_api_accessed == "no"
    assert report.database_accessed == "no"
    serialized = report.model_dump_json()
    assert "secret-hash" not in serialized
    assert "postgresql+asyncpg" not in serialized
    assert str(tmp_path / "session") not in serialized


def test_static_preflight_reports_stable_blockers(tmp_path: Path) -> None:
    configured = Settings(
        WATCHLIST_PATH=tmp_path / "missing.json",
        TELEGRAM_API_ID=None,
        TELEGRAM_API_HASH="",
        TELEGRAM_SESSION_NAME="",
        DATABASE_URL="",
    )

    report = StaticStartupPreflight(
        configured,
        dependency_probe=lambda: False,
        writable_probe=lambda path: False,
    ).run()

    assert report.status == "fail"
    assert "watchlist_not_found" in report.blockers
    assert "telethon_dependency_missing" in report.blockers
    assert "telegram_api_id_missing" in report.blockers
    assert "database_url_missing" in report.blockers
