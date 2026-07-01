"""Channel ORM model — per ARCHITECTURE.md V2.1-final §2.1."""

from datetime import datetime

from sqlalchemy import BigInteger, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    tg_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_type: Mapped[str] = mapped_column(String(20), default="telegram")
    status: Mapped[str] = mapped_column(String(20), default="active")
    rule_profile: Mapped[str | None] = mapped_column(String(50), nullable=True)
    special_parser: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
