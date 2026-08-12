"""heal stale foreign keys that still target the dead api_keys table

Revision ID: d4f6a2b8e1c3
Revises: c8d3e5f7a1b2
Create Date: 2026-08-12 10:52:00.000000

Issue #36 — email verification silently failed in prod because three foreign
keys still referenced the old ``api_keys`` table (renamed to ``agents`` during
the herd-inbox -> Stoa migration). On the drifted prod DB (which predates the
baseline and was stamped rather than built by it), ``api_keys`` still exists but
is empty, so every INSERT into these tables violated the FK:

  - memberships.agent_id            -> api_keys.id   (breaks verify auto-join + all joins)
  - groups.created_by_agent_id      -> api_keys.id   (breaks group creation)
  - join_requests.agent_id          -> api_keys.id   (breaks join requests)

This migration repoints those FKs to ``agents.id``.

Guarded/idempotent (applies the alembic lesson: inspect the live schema, never
assume it):
  * Only acts when a FK actually references ``api_keys`` for the given column,
    so it is a safe no-op on any DB already built correctly from the baseline
    (fresh deploys, test DBs).
  * SQLite (test/dev) builds its schema from the models via ``create_all`` and
    therefore already targets ``agents``; SQLite also cannot ALTER constraints
    in place, so this migration is intentionally a Postgres-only operation and a
    no-op elsewhere.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f6a2b8e1c3"
down_revision: str | Sequence[str] | None = "c8d3e5f7a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, column, canonical constraint name) for each stale FK.
_STALE_FKS = [
    ("memberships", "agent_id", "memberships_agent_id_fkey"),
    ("groups", "created_by_agent_id", "groups_created_by_agent_id_fkey"),
    ("join_requests", "agent_id", "join_requests_agent_id_fkey"),
]


def _repoint_fks(referred_from: str, referred_to: str) -> None:
    """Repoint each stale FK from ``referred_from`` to ``referred_to`` table.

    Name-agnostic: finds the actual constraint that references ``referred_from``
    for the target column and drops it by its real name before recreating a FK
    with the canonical name against ``referred_to``. No-op when no such FK
    exists, which makes this safe to re-run.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite/other: schema built from models already targets ``agents``.
        return

    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table, column, canonical_name in _STALE_FKS:
        if table not in existing_tables:
            continue
        for fk in inspector.get_foreign_keys(table):
            if fk.get("referred_table") == referred_from and column in (
                fk.get("constrained_columns") or []
            ):
                if fk.get("name"):
                    op.drop_constraint(fk["name"], table, type_="foreignkey")
                op.create_foreign_key(
                    canonical_name, table, referred_to, [column], ["id"]
                )
                break


def upgrade() -> None:
    """Repoint memberships/groups/join_requests FKs api_keys -> agents."""
    _repoint_fks(referred_from="api_keys", referred_to="agents")


def downgrade() -> None:
    """Reverse: repoint agents -> api_keys, but only if api_keys still exists.

    On DBs where the vestigial ``api_keys`` table is absent (anything built from
    the baseline), the reverse target does not exist, so this is a no-op rather
    than reintroducing a broken FK to a missing table.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    if "api_keys" not in set(inspector.get_table_names()):
        return
    _repoint_fks(referred_from="agents", referred_to="api_keys")
