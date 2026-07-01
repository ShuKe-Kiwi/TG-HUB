"""Read-only Resource query view models."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _QueryViewModel(BaseModel):
    """Immutable scalar-only base for Resource query projections."""

    model_config = ConfigDict(frozen=True)


class ResourceListItem(_QueryViewModel):
    resource_id: int
    work_id: int
    title: str
    work_title: str
    content_type: str
    resource_type: str
    episode_no: int | None
    season_no: int | None
    episode_range: str
    year: int | None
    quality: str | None
    source_count: int
    last_seen_at: datetime


class LinkView(_QueryViewModel):
    link_id: int
    provider: str
    url: str | None
    access_code: str | None
    password: str | None
    link_type: str
    status: str


class SourceView(_QueryViewModel):
    source_id: int
    raw_message_id: int
    channel_id: int
    channel_title: str | None
    channel_username: str | None
    match_type: str
    confidence: float
    matched_reason: str
    parser_version: str
    rule_version: str
    detected_at: datetime


class ResourceDetail(ResourceListItem):
    title_norm: str
    work_title_norm: str
    description: str | None
    tags: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    links: list[LinkView] = Field(default_factory=list)
    sources: list[SourceView] = Field(default_factory=list)
