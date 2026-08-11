"""add missing agent profile columns (idempotent)

Revision ID: e9fb00a98097
Revises: c55dbf72bdd9
Create Date: 2026-08-11 14:05:13.486501

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e9fb00a98097'
down_revision: str | Sequence[str] | None = 'c55dbf72bdd9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _profile_columns() -> list[sa.Column]:
    """Fresh Column objects for the Phase 1 agent profile fields.

    Built by a factory (not module-level singletons) so upgrade and downgrade
    each get unattached Column instances.
    """
    return [
        sa.Column("avatar_url", sa.String(length=500), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=True),
        sa.Column("links", sa.JSON(), nullable=True),
        sa.Column("operator_name", sa.String(length=280), nullable=True),
        sa.Column("operator_email", sa.String(length=320), nullable=True),
        sa.Column("last_active_at", sa.DateTime(), nullable=True),
        # profile_public is NOT NULL in the model; server_default backfills
        # existing rows to True. Kept in place (harmless) to avoid a follow-up
        # ALTER; the ORM sets the value explicitly on new rows.
        sa.Column(
            "profile_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    ]


def upgrade() -> None:
    """Add any missing agent profile columns.

    The baseline migration already defines all of these, but production was
    created by an earlier create_all() (pre-profile) and then
    ``alembic stamp head``-ed, so the baseline never actually ran there and
    these columns are missing. Adding only absent columns makes this a safe
    no-op on any DB that already has the full baseline schema.
    """
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("agents")}
    for column in _profile_columns():
        if column.name not in existing:
            op.add_column("agents", column)


def downgrade() -> None:
    """Drop the agent profile columns if present."""
    bind = op.get_bind()
    existing = {col["name"] for col in sa.inspect(bind).get_columns("agents")}
    for column in reversed(_profile_columns()):
        if column.name in existing:
            op.drop_column("agents", column.name)
