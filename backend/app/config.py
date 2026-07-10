"""Application configuration — loaded from environment variables / .env file."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    APP_NAME: str = "tg-hub"
    APP_ENV: str = "development"
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000

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


settings = Settings()
