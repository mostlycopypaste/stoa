"""add verification_tier to agents and vouches table (idempotent)

Revision ID: c8d3e5f7a1b2
Revises: b7e2f1a9c3d5
Create Date: 2026-08-12 10:05:00.000000

Issue #20 — verification tiers. Adds ``agents.verification_tier`` (0 unverified,
1 verified, 2 vouched) and a ``vouches`` table tracking who vouched for whom.
Backfills existing verified agents to Tier 1. Guarded/idempotent so it is a safe
no-op on a DB that already has these objects (applies the alembic lesson: never
assume the live schema — inspect it).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d3e5f7a1b2"
down_revision: str | Sequence[str] | None = "b7e2f1a9c3d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add verification_tier + vouches, backfill verified agents to Tier 1."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "verification_tier" not in agent_cols:
        op.add_column(
            "agents",
            sa.Column(
                "verification_tier",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )
        # Backfill: any already-verified agent is at least Tier 1.
        op.execute(
            "UPDATE agents SET verification_tier = 1 "
            "WHERE is_verified AND verification_tier = 0"
        )

    if "vouches" not in inspector.get_table_names():
        op.create_table(
            "vouches",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("voucher_email", sa.String(length=255), nullable=False),
            sa.Column("vouchee_email", sa.String(length=255), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("voucher_email", "vouchee_email", name="uq_vouch_pair"),
        )
        op.create_index("idx_vouches_vouchee", "vouches", ["vouchee_email"])


def downgrade() -> None:
    """Drop vouches table and agents.verification_tier if present."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "vouches" in inspector.get_table_names():
        op.drop_index("idx_vouches_vouchee", table_name="vouches")
        op.drop_table("vouches")

    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "verification_tier" in agent_cols:
        op.drop_column("agents", "verification_tier")
