"""mentions table for @mention tracking (issue #14)

Revision ID: a1b2c3d4e5f6
Revises: e7a3f9c1b4d2
Create Date: 2026-08-14

Creates the ``mentions`` table to store @mention records linking posts
and comments to the agents mentioned in them. Guarded/idempotent: inspects
the live schema before making any change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "e7a3f9c1b4d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create mentions table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "mentions" not in table_names:
        op.create_table(
            "mentions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("post_id", sa.Integer(), nullable=True),
            sa.Column("comment_id", sa.Integer(), nullable=True),
            sa.Column("mentioned_agent_id", sa.Integer(), nullable=False),
            sa.Column("mentioned_by", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["comment_id"], ["comments.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["mentioned_agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("idx_mentions_post_id", "mentions", ["post_id"], unique=False)
        op.create_index("idx_mentions_comment_id", "mentions", ["comment_id"], unique=False)
        op.create_index(
            "idx_mentions_agent_id", "mentions", ["mentioned_agent_id"], unique=False
        )


def downgrade() -> None:
    """Drop mentions table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "mentions" in table_names:
        op.drop_index("idx_mentions_agent_id", table_name="mentions")
        op.drop_index("idx_mentions_comment_id", table_name="mentions")
        op.drop_index("idx_mentions_post_id", table_name="mentions")
        op.drop_table("mentions")