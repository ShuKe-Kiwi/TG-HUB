"""Pure key and fingerprint builders."""

import hashlib
import re
from typing import Literal, cast

ContentType = Literal["drama", "movie", "variety", "anime", "other"]
EpisodeKind = Literal[
    "single_episode",
    "episode_range",
    "full",
    "unknown",
]

CONTENT_TYPES: frozenset[str] = frozenset(
    {"drama", "movie", "variety", "anime", "other"}
)

_EPISODE_RANGE_RE = re.compile(r"ep([1-9]\d*)(?:-([1-9]\d*))?")
_SEASON_EPISODE_RANGE_RE = re.compile(
    r"s(\d{2})e([1-9]\d*)(?:-([1-9]\d*))?"
)


def normalize_content_type(value: str) -> ContentType:
    """Return an architecture-defined content type, falling back to other."""
    normalized = (value or "").strip().casefold()
    if normalized not in CONTENT_TYPES:
        normalized = "other"
    return cast(ContentType, normalized)


def validate_episode_range(value: str) -> str:
    """Validate an already-canonical episode range."""
    if value in {"all", "unknown"}:
        return value

    episode_match = _EPISODE_RANGE_RE.fullmatch(value)
    if episode_match is not None:
        start = int(episode_match.group(1))
        end = int(episode_match.group(2)) if episode_match.group(2) else None
        if end is None or end >= start:
            return value

    season_match = _SEASON_EPISODE_RANGE_RE.fullmatch(value)
    if season_match is not None:
        season = int(season_match.group(1))
        start = int(season_match.group(2))
        end = int(season_match.group(3)) if season_match.group(3) else None
        if season > 0 and (end is None or end >= start):
            return value

    raise ValueError(f"invalid canonical episode_range: {value}")


def build_work_key(
    content_type: str,
    title_norm: str,
    year: int | None,
) -> str:
    year_token = str(year) if year is not None else "unknown"
    return f"{normalize_content_type(content_type)}:{title_norm}:{year_token}"


def build_resource_key(
    content_type: str,
    title_norm: str,
    episode_range: str,
) -> str:
    canonical_range = validate_episode_range(episode_range)
    return (
        f"{normalize_content_type(content_type)}:"
        f"{title_norm}:{canonical_range}"
    )


def build_episode_key(
    content_type: str,
    title_norm: str,
    episode_kind: EpisodeKind,
    season_no: int | None,
    episode_no: int | None,
) -> str | None:
    """Build a key only for a single episode."""
    if episode_kind != "single_episode":
        return None
    if episode_no is None or episode_no < 1:
        raise ValueError("single_episode requires a positive episode_no")

    season = season_no if season_no is not None else 1
    if season < 1:
        raise ValueError("season_no must be positive")

    return (
        f"{normalize_content_type(content_type)}:{title_norm}:"
        f"s{season:02d}:e{episode_no:02d}"
    )


def build_content_fingerprint(
    title_norm: str,
    episode_range: str,
    content_type: str,
    year: int | None,
) -> str:
    """sha256(title_norm + episode_range + type + year_or_unknown)."""
    canonical_range = validate_episode_range(episode_range)
    canonical_type = normalize_content_type(content_type)
    year_token = str(year) if year is not None else "unknown"
    payload = f"{title_norm}{canonical_range}{canonical_type}{year_token}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
