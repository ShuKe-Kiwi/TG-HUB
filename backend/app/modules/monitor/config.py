"""File-backed monitor configuration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings


class SourceChannelConfig(BaseModel):
    """One configured upstream channel reference."""

    model_config = ConfigDict(frozen=True)

    ref: str
    enabled: bool = True

    @field_validator("ref")
    @classmethod
    def normalize_ref(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source channel ref cannot be empty")
        return normalized


class WatchTitleConfig(BaseModel):
    """One title the monitor should keep."""

    model_config = ConfigDict(frozen=True)

    title: str
    enabled: bool = True
    aliases: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("watch title cannot be empty")
        return normalized

    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if item.strip()]

class WatchlistConfig(BaseModel):
    """Configured sources and watched titles."""

    model_config = ConfigDict(frozen=True)

    source_channels: list[SourceChannelConfig] = Field(default_factory=list)
    watch_titles: list[WatchTitleConfig] = Field(default_factory=list)

    def enabled_source_refs(self) -> tuple[str, ...]:
        return tuple(
            channel.ref for channel in self.source_channels if channel.enabled
        )

    def enabled_watch_titles(self) -> tuple[WatchTitleConfig, ...]:
        return tuple(title for title in self.watch_titles if title.enabled)


def _read_json_file(path: str | Path) -> Any:
    resolved_path = Path(path).expanduser()
    with resolved_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_watchlist(path: str | Path | None = None) -> WatchlistConfig:
    """Load the watchlist JSON from disk and validate its shape."""
    raw_data = _read_json_file(path or settings.WATCHLIST_PATH)
    return WatchlistConfig.model_validate(raw_data)
