from pathlib import Path

from app.config import Settings, load_settings
from app.deploy import validate_production_config


def test_env_file_is_parsed_without_shell_execution(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    env_file = tmp_path / "production.env"
    env_file.write_text(
        "TELEGRAM_API_HASH='value with spaces $(touch "
        f"{marker})'\nCONFIG_SCHEMA_VERSION=1\n",
        encoding="utf-8",
    )

    loaded = load_settings(env_file)

    assert loaded.TELEGRAM_API_HASH.startswith("value with spaces $(touch")
    assert not marker.exists()


def test_production_contract_accepts_loopback_and_private_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".tg-hub"
    configured = Settings(
        APP_ENV="production",
        ADMIN_BIND_HOST="127.0.0.1",
        WATCHLIST_PATH=root / "watchlist.json",
        TELEGRAM_SESSION_NAME=str(root / "telethon"),
        HEARTBEAT_PATH=root / "runtime" / "heartbeat.jsonl",
        LOG_DIR=root / "logs",
        BACKUP_DIR=root / "backups",
    )

    report = validate_production_config(configured, home=tmp_path)

    assert report.status == "pass"
    assert report.blockers == []


def test_production_contract_rejects_remote_bind_and_external_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".tg-hub"
    configured = Settings(
        APP_ENV="production",
        ADMIN_BIND_HOST="0.0.0.0",
        WATCHLIST_PATH=tmp_path / "outside.json",
        TELEGRAM_SESSION_NAME=str(root / "telethon"),
        HEARTBEAT_PATH=root / "runtime" / "heartbeat.jsonl",
        LOG_DIR=root / "logs",
        BACKUP_DIR=root / "backups",
    )

    report = validate_production_config(configured, home=tmp_path)

    assert report.status == "fail"
    assert report.blockers == [
        "ADMIN_BIND_NOT_LOOPBACK",
        "PRIVATE_PATH_OUTSIDE_ROOT",
    ]
