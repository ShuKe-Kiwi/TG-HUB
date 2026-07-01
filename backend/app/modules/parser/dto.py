"""Parser DTOs — per ARCHITECTURE.md V2.1-final §3.

Three DTOs:
- ParsedLink: extracted provider+url+share_id+access_code+password
- ParsedMetadata: parsed episode_no, season_no, episode_range, quality, etc.
- ParsedResource: aggregate with title, description, resource_type, tags, confidence
"""

from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LinkProvider(str, Enum):
    """Supported cloud storage providers."""

    QUARK = "quark"
    BAIDU = "baidu"
    XUNLEI = "xunlei"
    ALIYUN = "aliyun"
    MEGA = "mega"
    GOOGLE = "google"
    OTHER = "other"


class ParsedLink(BaseModel):
    """Extracted link from a Telegram message.

    Attributes:
        provider: Detected provider (e.g., "quark", "baidu").
        original_text: The original substring matched in raw_text.
        url: The full share URL.
        share_id: Platform resource ID (if extractable).
        access_code: Extraction code / password (e.g., quark code).
        password: Special password (e.g., xunlei command).
        link_type: Type of link (url / command / magnet / text_code).
        confidence: Confidence score for this link detection (0-1).
    """

    provider: LinkProvider
    original_text: str
    url: str | None = None
    share_id: str | None = None
    access_code: str | None = None
    password: str | None = None
    link_type: str = "url"
    confidence: float = 1.0

    model_config = ConfigDict(use_enum_values=True)

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> LinkProvider:
        """Map unsupported provider names to the architecture-defined fallback."""
        if isinstance(value, LinkProvider):
            return value
        try:
            return LinkProvider(value)
        except (TypeError, ValueError):
            return LinkProvider.OTHER


class ParsedMetadata(BaseModel):
    """Parsed metadata about a resource.

    Attributes:
        episode_no: Episode number (e.g., 10 for "第10集").
        season_no: Season number (e.g., 2 for "第二季").
        episode_range: Episode range (e.g., "ep1-10", "all", "ep1").
        year: Release year.
        quality: Quality tag (e.g., "1080p", "4K").
        file_size: File size (e.g., "12.5GB").
        language: Language tag (e.g., "国语", "粤语").
        subtitle: Subtitle tag (e.g., "中字", "内嵌").
    """

    episode_no: int | None = None
    season_no: int | None = None
    episode_range: str | None = None
    year: int | None = None
    quality: str | None = None
    file_size: str | None = None
    language: str | None = None
    subtitle: str | None = None


class ParsedResource(BaseModel):
    """Complete parsed result from a RawMessage.

    Attributes:
        title: Extracted resource title.
        raw_title: Original title before parsing.
        description: Resource description.
        resource_type: Resource type (drama / movie / variety / anime / other).
        links: List of ParsedLink instances.
        metadata: ParsedMetadata instance.
        tags: List of additional tags (e.g., "HDR", "杜比视界").
        confidence: Overall confidence score (0-1).
        parser_version: Parser implementation version.
        rule_version: Rule version used for parsing.
    """

    title: str
    raw_title: str
    description: str | None = None
    resource_type: str = "drama"
    links: list[ParsedLink] = Field(default_factory=list)
    metadata: ParsedMetadata | None = None
    tags: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    parser_version: str = ""
    rule_version: str = ""
