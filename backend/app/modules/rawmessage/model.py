"""RawMessage ORM model — per ARCHITECTURE.md V2.1-final §2.2.

Key constraints:
- Unique: (channel_id, tg_message_id) — primary dedup
- content_hash: regular index only, NOT unique (backup dedup, allows duplicates)
- Three independent status dimensions: ingest_status, parse_status, dedup_status
- Initial state after successful ingest:
    ingest_status = stored
    parse_status  = parse_pending
    dedup_status  = dedup_pending
    parse_attempts = 0
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RawMessage(Base):
    __tablename__ = "raw_messages"

    # Primary key
    id: Mapped[int] = mapped_column(primary_key=True)

    # Foreign key to channel
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )

    # Telegram message identity
    tg_message_id: Mapped[int] = mapped_column(BigInteger)

    # Raw content — preserved verbatim
    raw_text: Mapped[str] = mapped_column(Text, default="")
    raw_media_refs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    raw_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Backup dedup hash — regular index only, NOT unique
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    # Timestamps from Telegram
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Three independent status dimensions
    ingest_status: Mapped[str] = mapped_column(String(20), default="stored")
    parse_status: Mapped[str] = mapped_column(String(20), default="parse_pending")
    dedup_status: Mapped[str] = mapped_column(String(20), default="dedup_pending")

    # Parse metadata
    parsed_data: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    rule_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parse_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_parsed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Record timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Primary dedup constraint — (channel_id, tg_message_id)
    __table_args__ = (
        UniqueConstraint("channel_id", "tg_message_id", name="uq_rawmsg_channel_tgmsg"),
    )
