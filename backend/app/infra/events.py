"""Scalar-only domain event contracts."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base contract shared by all in-process domain events."""

    occurred_at: datetime = field(default_factory=_utc_now)


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceCreated(DomainEvent):
    """A new resource was persisted successfully."""

    resource_id: int
    work_id: int
    raw_message_id: int
    source_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ResourceMerged(DomainEvent):
    """New source or link data was merged into an existing resource."""

    resource_id: int
    work_id: int
    raw_message_id: int
    source_count: int
    created_link_count: int
    created_source: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class RawMessageFailed(DomainEvent):
    """A raw message could not produce a valid parser result."""

    raw_message_id: int
    parse_attempts: int
    error: str
