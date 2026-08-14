"""Change comments.in_reply_to FK from CASCADE to SET NULL (issue #15).

Revision ID: b3c5d7e9f1a2
Revises: e7a3f9c1b4d2
Create Date: 2026-08-14

On Postgres: drop and recreate the FK constraint with ON DELETE SET NULL
so deleting a comment orphan-roots its replies instead of cascade-deleting
them.  On SQLite this is a no-op (FK constraints are not enforced by default
and create_all builds the correct constraint from the model).
"""

from alembic import op

revision = "b3c5d7e9f1a2"
down_revision = "e7a3f9c1b4d2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Recreate in_reply_to FK with ON DELETE SET NULL (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Drop the old CASCADE FK and recreate with SET NULL.
    op.drop_constraint("comments_in_reply_to_fkey", "comments", type_="foreignkey")
    op.create_foreign_key(
        "comments_in_reply_to_fkey",
        "comments",
        "comments",
        ["in_reply_to"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Revert to CASCADE."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_constraint("comments_in_reply_to_fkey", "comments", type_="foreignkey")
    op.create_foreign_key(
        "comments_in_reply_to_fkey",
        "comments",
        "comments",
        ["in_reply_to"],
        ["id"],
        ondelete="CASCADE",
    )