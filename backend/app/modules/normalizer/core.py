"""Pure ParsedResource normalization for P3-A."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from app.modules.normalizer.fingerprint import (
    ContentType,
    EpisodeKind,
    build_content_fingerprint,
    build_episode_key,
    build_resource_key,
    build_work_key,
    normalize_content_type,
)
from app.modules.parser.dto import ParsedMetadata, ParsedResource

EpisodeSource = Literal["metadata", "title", "unknown"]

_BOUNDARY = r"(?=$|[\s,，。:：;；/|_\-])"
_NUMBER_TOKEN = r"[一二三四五六七八九十百千\d]+"

_SEASON_EPISODE_RE = re.compile(
    rf"(?<![a-z0-9])s0*(\d+)e0*(\d+)"
    rf"(?:[-—~]0*(\d+))?{_BOUNDARY}",
    re.IGNORECASE,
)
_UPDATE_TO_RE = re.compile(rf"更新至\s*0*(\d+)\s*集{_BOUNDARY}")
_EPISODE_RANGE_RE = re.compile(
    rf"第\s*({_NUMBER_TOKEN})\s*[-—~至]\s*"
    rf"({_NUMBER_TOKEN})\s*集{_BOUNDARY}"
)
_EPISODE_RE = re.compile(rf"第\s*({_NUMBER_TOKEN})\s*集{_BOUNDARY}")
_ENGLISH_EPISODE_RE = re.compile(
    rf"(?<![a-z0-9])(?:ep|e)0*(\d+)"
    rf"(?:[-—~]0*(\d+))?{_BOUNDARY}",
    re.IGNORECASE,
)
_FULL_RE = re.compile(rf"全集{_BOUNDARY}")
_SEASON_RE = re.compile(rf"第\s*({_NUMBER_TOKEN})\s*季{_BOUNDARY}")

_TRAILING_TWO_DIGIT_RE = re.compile(
    r"(?:^|[\s,，:：;；/|_\-])(\d{2})$"
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}(?:年)?", re.IGNORECASE)
_QUALITY_RE = re.compile(
    r"(?:2160p|1080[pi]|720p|4k|8k|蓝光)",
    re.IGNORECASE,
)
_FILE_SIZE_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kb|mb|gb|tb)",
    re.IGNORECASE,
)

_PREFIX_RE = re.compile(
    r"^(?:(?:资源|分享|更新)(?=[\s:：\-_|])[\s:：\-_|]+)+",
    re.IGNORECASE,
)
_DECORATIONS = "【】[]〔〕〖〗《》★☆◆◇●○▪■□▶►"
_DECORATION_TRANSLATION = str.maketrans(
    {character: " " for character in _DECORATIONS}
)
_EDGE_NOISE = " \t\r\n,，、:：;；/|_-"

_CANONICAL_EP_RE = re.compile(r"ep0*(\d+)(?:[-—~]0*(\d+))?")
_CANONICAL_SEASON_EP_RE = re.compile(
    r"s0*(\d+)e0*(\d+)(?:[-—~]0*(\d+))?"
)

_CN_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}


@dataclass(frozen=True)
class NormalizedEpisode:
    episode_kind: EpisodeKind
    episode_source: EpisodeSource
    season_no: int | None
    episode_no: int | None
    episode_range: str


@dataclass(frozen=True)
class NormalizedResource:
    title_norm: str
    content_type: ContentType
    year: int | None
    episode_kind: EpisodeKind
    episode_source: EpisodeSource
    season_no: int | None
    episode_no: int | None
    episode_range: str
    work_key: str
    resource_key: str
    episode_key: str | None
    content_fingerprint: str


def _prepare_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = text.translate(_DECORATION_TRANSLATION)
    text = re.sub(r"\s+", " ", text).strip()
    text = _PREFIX_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _to_positive_int(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if value.isdigit():
        number = int(value)
        return number if number > 0 else None

    total = 0
    current = 0
    for character in value:
        if character in _CN_DIGITS:
            current = _CN_DIGITS[character]
        elif character in _CN_UNITS:
            unit = _CN_UNITS[character]
            total += (current or 1) * unit
            current = 0
        else:
            return None
    number = total + current
    return number if number > 0 else None


def _can_infer_trailing_number(text: str) -> bool:
    if _YEAR_RE.search(text):
        return False
    if _QUALITY_RE.search(text):
        return False
    if _FILE_SIZE_RE.search(text):
        return False

    match = _TRAILING_TWO_DIGIT_RE.search(text)
    return bool(match and int(match.group(1)) > 0)


def _canonical_episode(
    episode_range: str,
    source: EpisodeSource,
) -> NormalizedEpisode | None:
    value = (
        unicodedata.normalize("NFKC", episode_range or "")
        .casefold()
        .replace(" ", "")
        .replace("—", "-")
        .replace("~", "-")
    )
    if value in {"all", "full"}:
        return NormalizedEpisode("full", source, None, None, "all")
    if value == "unknown":
        return None

    season_match = _CANONICAL_SEASON_EP_RE.fullmatch(value)
    if season_match:
        season = _to_positive_int(season_match.group(1))
        start = _to_positive_int(season_match.group(2))
        end = _to_positive_int(season_match.group(3))
        if season is None or start is None:
            return None
        if end is not None:
            if end < start:
                return None
            return NormalizedEpisode(
                "episode_range",
                source,
                season,
                None,
                f"s{season:02d}e{start}-{end}",
            )
        return NormalizedEpisode(
            "single_episode",
            source,
            season,
            start,
            f"s{season:02d}e{start}",
        )

    episode_match = _CANONICAL_EP_RE.fullmatch(value)
    if episode_match:
        start = _to_positive_int(episode_match.group(1))
        end = _to_positive_int(episode_match.group(2))
        if start is None:
            return None
        if end is not None:
            if end < start:
                return None
            return NormalizedEpisode(
                "episode_range",
                source,
                None,
                None,
                f"ep{start}-{end}",
            )
        return NormalizedEpisode(
            "single_episode",
            source,
            None,
            start,
            f"ep{start}",
        )
    return None


def _episode_from_metadata(
    metadata: ParsedMetadata | None,
) -> NormalizedEpisode | None:
    if metadata is None:
        return None

    if metadata.episode_range:
        normalized = _canonical_episode(
            metadata.episode_range,
            "metadata",
        )
        if normalized is not None:
            return normalized

    episode_no = _to_positive_int(metadata.episode_no)
    if episode_no is None:
        return None

    season_no = _to_positive_int(metadata.season_no)
    episode_range = (
        f"s{season_no:02d}e{episode_no}"
        if season_no is not None
        else f"ep{episode_no}"
    )
    return NormalizedEpisode(
        "single_episode",
        "metadata",
        season_no,
        episode_no,
        episode_range,
    )


def _episode_from_title(title: str) -> NormalizedEpisode:
    text = _prepare_text(title)

    season_episode_match = _SEASON_EPISODE_RE.search(text)
    if season_episode_match:
        season = int(season_episode_match.group(1))
        start = int(season_episode_match.group(2))
        end = (
            int(season_episode_match.group(3))
            if season_episode_match.group(3)
            else None
        )
        if season > 0 and start > 0 and (end is None or end >= start):
            if end is not None:
                return NormalizedEpisode(
                    "episode_range",
                    "title",
                    season,
                    None,
                    f"s{season:02d}e{start}-{end}",
                )
            return NormalizedEpisode(
                "single_episode",
                "title",
                season,
                start,
                f"s{season:02d}e{start}",
            )

    season_match = _SEASON_RE.search(text)
    season = (
        _to_positive_int(season_match.group(1))
        if season_match is not None
        else None
    )

    update_match = _UPDATE_TO_RE.search(text)
    if update_match and int(update_match.group(1)) > 0:
        end = int(update_match.group(1))
        episode_range = (
            f"s{season:02d}e1-{end}" if season is not None else f"ep1-{end}"
        )
        return NormalizedEpisode(
            "episode_range",
            "title",
            season,
            None,
            episode_range,
        )

    range_match = _EPISODE_RANGE_RE.search(text)
    if range_match:
        start = _to_positive_int(range_match.group(1))
        end = _to_positive_int(range_match.group(2))
        if start is not None and end is not None and end >= start:
            episode_range = (
                f"s{season:02d}e{start}-{end}"
                if season is not None
                else f"ep{start}-{end}"
            )
            return NormalizedEpisode(
                "episode_range",
                "title",
                season,
                None,
                episode_range,
            )

    episode_match = _EPISODE_RE.search(text)
    if episode_match:
        episode = _to_positive_int(episode_match.group(1))
        if episode is not None:
            episode_range = (
                f"s{season:02d}e{episode}"
                if season is not None
                else f"ep{episode}"
            )
            return NormalizedEpisode(
                "single_episode",
                "title",
                season,
                episode,
                episode_range,
            )

    english_match = _ENGLISH_EPISODE_RE.search(text)
    if english_match and int(english_match.group(1)) > 0:
        episode = int(english_match.group(1))
        range_end = (
            int(english_match.group(2))
            if english_match.group(2)
            else None
        )
        if range_end is not None:
            if range_end >= episode:
                episode_range = (
                    f"s{season:02d}e{episode}-{range_end}"
                    if season is not None
                    else f"ep{episode}-{range_end}"
                )
                return NormalizedEpisode(
                    "episode_range",
                    "title",
                    season,
                    None,
                    episode_range,
                )
            return NormalizedEpisode(
                "unknown",
                "unknown",
                None,
                None,
                "unknown",
            )
        episode_range = (
            f"s{season:02d}e{episode}"
            if season is not None
            else f"ep{episode}"
        )
        return NormalizedEpisode(
            "single_episode",
            "title",
            season,
            episode,
            episode_range,
        )

    if _FULL_RE.search(text):
        return NormalizedEpisode("full", "title", None, None, "all")

    if _can_infer_trailing_number(text):
        match = _TRAILING_TWO_DIGIT_RE.search(text)
        assert match is not None
        episode = int(match.group(1))
        return NormalizedEpisode(
            "single_episode",
            "title",
            None,
            episode,
            f"ep{episode}",
        )

    return NormalizedEpisode("unknown", "unknown", None, None, "unknown")


def normalize_episode(
    title: str,
    metadata: ParsedMetadata | None = None,
) -> NormalizedEpisode:
    """Normalize episode identity with metadata taking precedence."""
    metadata_episode = _episode_from_metadata(metadata)
    if metadata_episode is not None:
        return metadata_episode
    return _episode_from_title(title)


def normalize_title(title: str) -> str:
    """Conservatively normalize a title without mutating its source."""
    text = _prepare_text(title)
    can_remove_trailing_number = _can_infer_trailing_number(text)

    for pattern in (
        _SEASON_EPISODE_RE,
        _UPDATE_TO_RE,
        _EPISODE_RANGE_RE,
        _EPISODE_RE,
        _ENGLISH_EPISODE_RE,
        _FULL_RE,
        _SEASON_RE,
    ):
        text = pattern.sub(" ", text)

    if can_remove_trailing_number:
        text = _TRAILING_TWO_DIGIT_RE.sub(" ", text)

    text = re.sub(r"\s+", " ", text).strip(_EDGE_NOISE)
    if not text:
        raise ValueError("title_norm must not be empty")
    return text


def normalize_resource(resource: ParsedResource) -> NormalizedResource:
    """Return an immutable normalized identity for a ParsedResource."""
    title_norm = normalize_title(resource.title)
    content_type = normalize_content_type(resource.resource_type)
    episode = normalize_episode(resource.title, resource.metadata)
    year = resource.metadata.year if resource.metadata is not None else None

    work_key = build_work_key(content_type, title_norm, year)
    resource_key = build_resource_key(
        content_type,
        title_norm,
        episode.episode_range,
    )
    episode_key = build_episode_key(
        content_type,
        title_norm,
        episode.episode_kind,
        episode.season_no,
        episode.episode_no,
    )
    content_fingerprint = build_content_fingerprint(
        title_norm,
        episode.episode_range,
        content_type,
        year,
    )

    return NormalizedResource(
        title_norm=title_norm,
        content_type=content_type,
        year=year,
        episode_kind=episode.episode_kind,
        episode_source=episode.episode_source,
        season_no=episode.season_no,
        episode_no=episode.episode_no,
        episode_range=episode.episode_range,
        work_key=work_key,
        resource_key=resource_key,
        episode_key=episode_key,
        content_fingerprint=content_fingerprint,
    )
