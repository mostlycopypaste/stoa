"""reconcile prod schema drift to models: heal posts table + drop vestigial tables

Revision ID: e1a7c9d24b60
Revises: d4f6a2b8e1c3
Create Date: 2026-08-12 11:20:00.000000

Issue #38 — posting 500'd in prod because ``posts.parent_post_id`` was missing.
Same root cause as #36 / #29: prod predates the alembic baseline and was
``stamp``ed, so migration DDL never ran against it and its live schema drifted
from the models (herd-inbox-era snowflake).

A full model-vs-prod audit found the remaining drift:

  posts:
    - MISSING parent_post_id  (model: nullable self-FK -> posts.id ON DELETE
      CASCADE, index idx_posts_parent_post_id)  <-- the 500
    - VESTIGIAL message_id (NOT NULL), space (NOT NULL), in_reply_to (nullable)
      -- not in the model; the two NOT-NULL columns also block inserts.
    (posts is empty in prod, so these column changes are zero-risk.)

  vestigial empty tables not in the models: api_keys, footer_messages,
  subscriptions (herd-inbox leftovers, 0 rows).

This migration reconciles prod to the models. Guarded/idempotent and
Postgres-only: it only touches objects that actually exist / are actually
missing, so it is a safe no-op on SQLite and on any DB already built correctly
from the baseline (fresh deploys, test DBs).

The vestigial-cleanup drops (columns + tables) are intentionally one-way: they
are empty herd-inbox leftovers and recreating them serves no purpose, so
``downgrade`` only reverses the model-relevant change (parent_post_id).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a7c9d24b60"
down_revision: str | Sequence[str] | None = "d4f6a2b8e1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VESTIGIAL_POST_COLS = ("message_id", "in_reply_to", "space")
_VESTIGIAL_TABLES = ("subscriptions", "footer_messages", "api_keys")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite/fresh DBs already match the models.

    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "posts" in tables:
        cols = {c["name"] for c in inspector.get_columns("posts")}

        # Drop vestigial columns (message_id/space are NOT NULL and block inserts).
        for vc in _VESTIGIAL_POST_COLS:
            if vc in cols:
                op.drop_column("posts", vc)

        # Add the self-referential parent_post_id the model expects.
        if "parent_post_id" not in cols:
            op.add_column(
                "posts", sa.Column("parent_post_id", sa.Integer(), nullable=True)
            )

        fk_names = {fk.get("name") for fk in inspector.get_foreign_keys("posts")}
        if "posts_parent_post_id_fkey" not in fk_names:
            op.create_foreign_key(
                "posts_parent_post_id_fkey",
                "posts",
                "posts",
                ["parent_post_id"],
                ["id"],
                ondelete="CASCADE",
            )

        idx_names = {ix["name"] for ix in inspector.get_indexes("posts")}
        if "idx_posts_parent_post_id" not in idx_names:
            op.create_index("idx_posts_parent_post_id", "posts", ["parent_post_id"])

    # Drop vestigial empty herd-inbox tables (no inbound model FKs after #36).
    for t in _VESTIGIAL_TABLES:
        if t in tables:
            op.drop_table(t)


def downgrade() -> None:
    """Reverse only the model-relevant change (parent_post_id).

    The vestigial column/table drops are intentionally not recreated -- they are
    empty herd-inbox leftovers with no purpose. This keeps up/down/re-up clean
    and idempotent without reintroducing dead schema.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    inspector = sa.inspect(bind)
    if "posts" not in set(inspector.get_table_names()):
        return

    idx_names = {ix["name"] for ix in inspector.get_indexes("posts")}
    if "idx_posts_parent_post_id" in idx_names:
        op.drop_index("idx_posts_parent_post_id", table_name="posts")

    fk_names = {fk.get("name") for fk in inspector.get_foreign_keys("posts")}
    if "posts_parent_post_id_fkey" in fk_names:
        op.drop_constraint("posts_parent_post_id_fkey", "posts", type_="foreignkey")

    cols = {c["name"] for c in inspector.get_columns("posts")}
    if "parent_post_id" in cols:
        op.drop_column("posts", "parent_post_id")
