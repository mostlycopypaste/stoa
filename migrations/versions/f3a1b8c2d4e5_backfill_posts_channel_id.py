"""backfill existing posts to #general channel

Revision ID: f3a1b8c2d4e5
Revises: e1a7c9d24b60
Create Date: 2026-08-12

Assigns channel_id=1 (#general) to all existing posts that have no
channel set. This is a one-time data backfill — the PostCreate schema
now accepts channel_id, but all posts created before this change have
NULL.
"""

from alembic import op

revision = "f3a1b8c2d4e5"
down_revision = "e1a7c9d24b60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE posts SET channel_id = 1 WHERE channel_id IS NULL"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE posts SET channel_id = NULL WHERE channel_id = 1 "
        "AND id IN (SELECT id FROM posts ORDER BY id LIMIT 2)"
    )