"""P4-A: create Resource Registry tables

Revision ID: 7b3f2a1c9d04
Revises: 1d86a0024d90
Create Date: 2026-07-01
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "7b3f2a1c9d04"
down_revision: Union[str, None] = "1d86a0024d90"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "works",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("title_norm", sa.String(length=500), nullable=False),
        sa.Column("type", sa.String(length=20), nullable=False),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("work_key", sa.String(length=200), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_key", name="uq_works_work_key"),
    )

    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("title_norm", sa.String(length=500), nullable=False),
        sa.Column("resource_type", sa.String(length=20), nullable=False),
        sa.Column("episode_no", sa.Integer(), nullable=True),
        sa.Column(
            "season_no",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=True,
        ),
        sa.Column("episode_range", sa.String(length=50), nullable=False),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("quality", sa.String(length=50), nullable=True),
        sa.Column("resource_key", sa.String(length=200), nullable=False),
        sa.Column("episode_key", sa.String(length=200), nullable=True),
        sa.Column(
            "content_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'active'"),
            nullable=False,
        ),
        sa.Column(
            "source_count",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["work_id"],
            ["works.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_key",
            name="uq_resources_resource_key",
        ),
    )
    op.create_index(
        "ix_resources_work_id",
        "resources",
        ["work_id"],
        unique=False,
    )
    op.create_index(
        "ix_resources_episode_key",
        "resources",
        ["episode_key"],
        unique=False,
    )
    op.create_index(
        "ix_resources_content_fingerprint",
        "resources",
        ["content_fingerprint"],
        unique=False,
    )

    op.create_table(
        "resource_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("original_url", sa.String(length=500), nullable=True),
        sa.Column("normalized_url", sa.String(length=500), nullable=True),
        sa.Column("url_hash", sa.String(length=64), nullable=False),
        sa.Column("share_id", sa.String(length=100), nullable=True),
        sa.Column("access_code", sa.String(length=50), nullable=True),
        sa.Column("password", sa.String(length=200), nullable=True),
        sa.Column(
            "link_type",
            sa.String(length=20),
            server_default=sa.text("'url'"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["resources.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_id",
            "provider",
            "url_hash",
            name="uq_resource_links_resource_provider_urlhash",
        ),
    )
    op.create_index(
        "ix_resource_links_provider",
        "resource_links",
        ["provider"],
        unique=False,
    )
    op.create_index(
        "ix_resource_links_share_id",
        "resource_links",
        ["share_id"],
        unique=False,
    )

    op.create_table(
        "resource_sources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("resource_id", sa.Integer(), nullable=False),
        sa.Column("raw_message_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("match_type", sa.String(length=20), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("matched_reason", sa.Text(), nullable=False),
        sa.Column(
            "parsed_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("parser_version", sa.String(length=20), nullable=False),
        sa.Column("rule_version", sa.String(length=20), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["channels.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["raw_message_id"],
            ["raw_messages.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resource_id"],
            ["resources.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "resource_id",
            "raw_message_id",
            name="uq_resource_sources_resource_rawmsg",
        ),
    )


def downgrade() -> None:
    op.drop_table("resource_sources")
    op.drop_index(
        "ix_resource_links_share_id",
        table_name="resource_links",
    )
    op.drop_index(
        "ix_resource_links_provider",
        table_name="resource_links",
    )
    op.drop_table("resource_links")
    op.drop_index(
        "ix_resources_content_fingerprint",
        table_name="resources",
    )
    op.drop_index(
        "ix_resources_episode_key",
        table_name="resources",
    )
    op.drop_index(
        "ix_resources_work_id",
        table_name="resources",
    )
    op.drop_table("resources")
    op.drop_table("works")
