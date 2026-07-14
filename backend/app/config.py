"""Application configuration loaded without shell evaluation."""

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    CONFIG_SCHEMA_VERSION: int = 1
    APP_NAME: str = "tg-hub"
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    ADMIN_BIND_HOST: str = "127.0.0.1"
    ADMIN_PORT: int = 8010

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://localhost/tg_hub_dev"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://localhost/tg_hub_test"

    # Logging
    LOG_LEVEL: str = "DEBUG"

    # Telegram
    TELEGRAM_WEBHOOK_SECRET: str = ""
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ALLOWED_CHAT_IDS: str = ""
    TELEGRAM_NOTIFY_CHAT_IDS: str = ""
    TELEGRAM_SESSION_NAME: str = "~/.tg-hub/telethon"
    TELEGRAM_API_ID: int | None = None
    TELEGRAM_API_HASH: str = ""

    # Monitor configuration
    WATCHLIST_PATH: Path = Path("~/.tg-hub/watchlist.json")
    MONITOR_HEARTBEAT_INTERVAL_SECONDS: int = 30
    MONITOR_RECONNECT_INITIAL_DELAY_SECONDS: int = 1
    MONITOR_RECONNECT_MAX_DELAY_SECONDS: int = 60
    MONITOR_RECONNECT_STABLE_RESET_SECONDS: int = 300
    MONITOR_DRAIN_TIMEOUT_SECONDS: int = 10
    MONITOR_MAX_INFLIGHT_EVENTS: int = 100
    MONITOR_AUTO_START: bool = False

    # Private runtime paths
    HEARTBEAT_PATH: Path = Path("~/.tg-hub/runtime/heartbeat.jsonl")
    LOG_DIR: Path = Path("~/.tg-hub/logs")
    BACKUP_DIR: Path = Path("~/.tg-hub/backups")
    RESTORE_VERIFY_TIMEOUT_SECONDS: int = 600


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load a dotenv file through pydantic, never through a shell."""
    selected = env_file or os.environ.get("TG_HUB_ENV_FILE") or ".env"
    return Settings(_env_file=Path(selected).expanduser())


settings = load_settings()
