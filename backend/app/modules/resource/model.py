"""Resource Registry ORM models — per ARCHITECTURE.md V2.1-final §2.3-2.6."""

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Work(Base):
    __tablename__ = "works"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    title_norm: Mapped[str] = mapped_column(String(500))
    type: Mapped[str] = mapped_column(String(20))
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        default=list,
        server_default=text("'{}'::text[]"),
    )
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    work_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default=text("'active'"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("work_key", name="uq_works_work_key"),
    )


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE")
    )
    title: Mapped[str] = mapped_column(String(500))
    title_norm: Mapped[str] = mapped_column(String(500))
    resource_type: Mapped[str] = mapped_column(String(20))

    episode_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    season_no: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=1,
        server_default=text("1"),
    )
    episode_range: Mapped[str] = mapped_column(String(50))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality: Mapped[str | None] = mapped_column(String(50), nullable=True)

    resource_key: Mapped[str] = mapped_column(String(200))
    episode_key: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    content_fingerprint: Mapped[str] = mapped_column(String(64))

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(Text),
        default=list,
        server_default=text("'{}'::text[]"),
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="active",
        server_default=text("'active'"),
    )
    source_count: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default=text("1"),
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("resource_key", name="uq_resources_resource_key"),
        Index("ix_resources_work_id", "work_id"),
        Index("ix_resources_episode_key", "episode_key"),
        Index("ix_resources_content_fingerprint", "content_fingerprint"),
    )


class ResourceLink(Base):
    __tablename__ = "resource_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE")
    )
    provider: Mapped[str] = mapped_column(String(20))
    original_text: Mapped[str] = mapped_column(Text)
    original_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    normalized_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
    )
    url_hash: Mapped[str] = mapped_column(String(64))
    share_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    access_code: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
    )
    password: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    link_type: Mapped[str] = mapped_column(
        String(20),
        default="url",
        server_default=text("'url'"),
    )
    status: Mapped[str] = mapped_column(
        String(20),
        default="unknown",
        server_default=text("'unknown'"),
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "resource_id",
            "provider",
            "url_hash",
            name="uq_resource_links_resource_provider_urlhash",
        ),
        Index("ix_resource_links_provider", "provider"),
        Index("ix_resource_links_share_id", "share_id"),
    )


class ResourceSource(Base):
    __tablename__ = "resource_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE")
    )
    raw_message_id: Mapped[int] = mapped_column(
        ForeignKey("raw_messages.id", ondelete="CASCADE")
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    match_type: Mapped[str] = mapped_column(String(20))
    confidence: Mapped[float] = mapped_column(Float)
    matched_reason: Mapped[str] = mapped_column(Text)
    parsed_snapshot: Mapped[dict | list] = mapped_column(JSONB)
    parser_version: Mapped[str] = mapped_column(String(20))
    rule_version: Mapped[str] = mapped_column(String(20))
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "resource_id",
            "raw_message_id",
            name="uq_resource_sources_resource_rawmsg",
        ),
    )
