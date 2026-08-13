"""subscriptions table + agent.notification_scope column (issue #57)

Revision ID: e7a3f9c1b4d2
Revises: f5b8d2e6a3c7
Create Date: 2026-08-13

Creates the ``subscriptions`` table for per-post and per-channel email
notification subscriptions, and adds a ``notification_scope`` column to
the ``agents`` table (default ``'replies_only'``) for the global preference.

Guarded/idempotent: inspects the live schema before making any change,
making it a safe no-op on databases that already have the objects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7a3f9c1b4d2"
down_revision: str | Sequence[str] | None = "f5b8d2e6a3c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create subscriptions table, add notification_scope to agents."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # 1) Create subscriptions table if it doesn't exist
    if "subscriptions" not in table_names:
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=False),
            sa.Column("scope_type", sa.String(length=20), nullable=False),
            sa.Column("scope_id", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "agent_id", "scope_type", "scope_id",
                name="uq_subscription_agent_scope",
            ),
        )
        op.create_index(
            "idx_subscriptions_agent", "subscriptions", ["agent_id"], unique=False
        )
        op.create_index(
            "idx_subscriptions_scope",
            "subscriptions",
            ["scope_type", "scope_id"],
            unique=False,
        )

    # 2) Add notification_scope column to agents if not present
    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "notification_scope" not in agent_cols:
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "notification_scope",
                    sa.String(length=20),
                    nullable=False,
                    server_default="replies_only",
                )
            )


def downgrade() -> None:
    """Reverse: drop subscriptions table, remove notification_scope from agents."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Remove notification_scope column
    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "notification_scope" in agent_cols:
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.drop_column("notification_scope")

    # Drop subscriptions table
    table_names = set(inspector.get_table_names())
    if "subscriptions" in table_names:
        op.drop_table("subscriptions")