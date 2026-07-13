"""Preserve RawMessage evidence when a Channel is deleted.

Revision ID: c4a8e7b1d2f0
Revises: 7b3f2a1c9d04
Create Date: 2026-07-14
"""

from typing import Sequence, Union

from alembic import op

revision: str = "c4a8e7b1d2f0"
down_revision: Union[str, None] = "7b3f2a1c9d04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "raw_messages_channel_id_fkey",
        "raw_messages",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "raw_messages_channel_id_fkey",
        "raw_messages",
        "channels",
        ["channel_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "raw_messages_channel_id_fkey",
        "raw_messages",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "raw_messages_channel_id_fkey",
        "raw_messages",
        "channels",
        ["channel_id"],
        ["id"],
        ondelete="CASCADE",
    )
