"""post_revisions table + post management fields (archive/move/pin)

Revision ID: f5b8d2e6a3c7
Revises: a4b7c9d1e2f3
Create Date: 2026-08-13

Issues #54 + #58 — Post editing with version history, and post management
(archive, move, pin). Creates the ``post_revisions`` table, adds
``pinned``/``pinned_at`` columns to ``posts``, and extends the
``check_status_values`` constraint to include 'archived' and 'deleted'.

Guarded/idempotent: inspects the live schema before making any change,
making it a safe no-op on databases that already have the objects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5b8d2e6a3c7"
down_revision: str | Sequence[str] | None = "a4b7c9d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create post_revisions, add pinned/pinned_at, extend status check."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # 1) Create post_revisions table if it doesn't exist
    table_names = set(inspector.get_table_names())
    if "post_revisions" not in table_names:
        op.create_table(
            "post_revisions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("post_id", sa.Integer(), nullable=False),
            sa.Column("revision_number", sa.Integer(), nullable=False),
            sa.Column("subject", sa.String(length=320), nullable=False),
            sa.Column("tldr", sa.String(length=280), nullable=False),
            sa.Column("body_markdown", sa.Text(), nullable=False),
            sa.Column("body_html", sa.Text(), nullable=False),
            sa.Column("token_cost", sa.Integer(), nullable=False),
            sa.Column("edited_by", sa.String(length=255), nullable=False),
            sa.Column("edited_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "idx_post_revisions_post_id", "post_revisions", ["post_id"], unique=False
        )

    # 2) Add pinned / pinned_at columns to posts if not present
    post_cols = {col["name"] for col in inspector.get_columns("posts")}
    if "pinned" not in post_cols or "pinned_at" not in post_cols:
        with op.batch_alter_table("posts", schema=None) as batch_op:
            if "pinned" not in post_cols:
                batch_op.add_column(
                    sa.Column(
                        "pinned",
                        sa.Boolean(),
                        nullable=False,
                        server_default=sa.text("false"),
                    )
                )
            if "pinned_at" not in post_cols:
                batch_op.add_column(
                    sa.Column("pinned_at", sa.DateTime(), nullable=True)
                )

    # 3) Extend check_status_values constraint to include 'archived' and 'deleted'
    # Re-inspect to get fresh constraint list (after possible batch alter)
    inspector = sa.inspect(bind)
    constraints = inspector.get_check_constraints("posts")
    status_constraint = next(
        (c for c in constraints if c["name"] == "check_status_values"), None
    )
    expected_def = "status IN ('open', 'closed', 'archived', 'deleted')"
    if status_constraint is not None:
        current_def = str(status_constraint["sqltext"]).strip()
        # Normalize whitespace for comparison
        normalized_current = " ".join(current_def.split())
        normalized_expected = " ".join(expected_def.split())
        if normalized_current != normalized_expected:
            with op.batch_alter_table("posts", schema=None) as batch_op:
                batch_op.drop_constraint("check_status_values", type_="check")
                batch_op.create_check_constraint(
                    "check_status_values", expected_def
                )
    else:
        # Constraint missing entirely — create it
        with op.batch_alter_table("posts", schema=None) as batch_op:
            batch_op.create_check_constraint("check_status_values", expected_def)

    # 4) Add index on pinned if not present
    indexes = {idx["name"] for idx in inspector.get_indexes("posts")}
    if "idx_posts_pinned" not in indexes:
        op.create_index("idx_posts_pinned", "posts", ["pinned"], unique=False)


def downgrade() -> None:
    """Reverse: drop post_revisions, remove pinned/pinned_at, restore old status check."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop index
    indexes = {idx["name"] for idx in inspector.get_indexes("posts")}
    if "idx_posts_pinned" in indexes:
        op.drop_index("idx_posts_pinned", table_name="posts")

    # Restore old status check constraint
    constraints = inspector.get_check_constraints("posts")
    status_constraint = next(
        (c for c in constraints if c["name"] == "check_status_values"), None
    )
    old_def = "status IN ('open', 'closed')"
    if status_constraint is not None:
        with op.batch_alter_table("posts", schema=None) as batch_op:
            batch_op.drop_constraint("check_status_values", type_="check")
            batch_op.create_check_constraint("check_status_values", old_def)

    # Drop pinned / pinned_at columns
    post_cols = {col["name"] for col in inspector.get_columns("posts")}
    if "pinned" in post_cols or "pinned_at" in post_cols:
        with op.batch_alter_table("posts", schema=None) as batch_op:
            if "pinned_at" in post_cols:
                batch_op.drop_column("pinned_at")
            if "pinned" in post_cols:
                batch_op.drop_column("pinned")

    # Drop post_revisions table
    table_names = set(inspector.get_table_names())
    if "post_revisions" in table_names:
        op.drop_table("post_revisions")