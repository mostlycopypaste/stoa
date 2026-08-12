"""add created_by to invites (idempotent)

Revision ID: b7e2f1a9c3d5
Revises: e9fb00a98097
Create Date: 2026-08-12 09:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2f1a9c3d5"
down_revision: str | Sequence[str] | None = "e9fb00a98097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add invites.created_by if absent.

    Tracks which agent (or "admin") minted an invite so per-agent invite
    creation can be rate-limited (issue #19). Nullable: existing invites and
    admin-minted invites may have no agent creator. Idempotent so it is a
    safe no-op on any DB that already has the column.
    """
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("invites")}
    if "created_by" not in existing:
        op.add_column(
            "invites",
            sa.Column("created_by", sa.String(length=255), nullable=True),
        )


def downgrade() -> None:
    """Drop invites.created_by if present."""
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("invites")}
    if "created_by" in existing:
        op.drop_column("invites", "created_by")
