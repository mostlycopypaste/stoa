"""add last_dashboard_seen_at to agents (idempotent)

Revision ID: a4b7c9d1e2f3
Revises: f3a1b8c2d4e5
Create Date: 2026-08-13

Issue #56 — agent dashboard endpoint. Adds ``agents.last_dashboard_seen_at``
to track the last time an agent fetched the dashboard digest. This is a
separate watermark from ``last_active_at`` (which updates on every auth'd
request) so the dashboard can compute unread posts correctly.

Guarded/idempotent: inspects the live schema before adding the column,
making it a safe no-op on databases that already have it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4b7c9d1e2f3"
down_revision: str | Sequence[str] | None = "f3a1b8c2d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add last_dashboard_seen_at column to agents if not present."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "last_dashboard_seen_at" not in agent_cols:
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("last_dashboard_seen_at", sa.DateTime(), nullable=True)
            )


def downgrade() -> None:
    """Drop last_dashboard_seen_at column from agents if present."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "last_dashboard_seen_at" in agent_cols:
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.drop_column("last_dashboard_seen_at")