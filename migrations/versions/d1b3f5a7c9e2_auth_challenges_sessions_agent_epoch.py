"""auth_challenges + auth_sessions tables, agents.auth_epoch (issue #134 Phase A)

Revision ID: d1b3f5a7c9e2
Revises: 0c038982f158
Create Date: 2026-10-02

Tiered agent auth, Phase A: email-challenge sessions (the lockout-killer
baseline). Two new tables and one agent column:

- ``auth_challenges`` — single-use emailed codes (digest-only storage),
  purpose-bound (mint/revoke) and epoch-bound (Q5 state machine:
  requested -> challenged -> consumed | expired).
- ``auth_sessions`` — Tier-1 session tokens minted by a verified challenge
  (opaque 256-bit, stored digest-only, TTL 24 h, state verified -> revoked).
- ``agents.auth_epoch`` — current auth epoch; revoke conditionally advances
  it (CAS) and invalidates every live session digest in the same
  transaction, so no credential derived from epoch N survives the committed
  N -> N+1 transition.

Guarded/idempotent: inspects the live schema before making any change,
making it a safe no-op on databases that already have the objects.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1b3f5a7c9e2"
down_revision: str | Sequence[str] | None = "0c038982f158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create auth_challenges + auth_sessions, add agents.auth_epoch."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "auth_challenges" not in table_names:
        op.create_table(
            "auth_challenges",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=False),
            sa.Column("agent_email", sa.String(length=255), nullable=False),
            sa.Column("purpose", sa.String(length=20), nullable=False),
            sa.Column("state", sa.String(length=20), nullable=False),
            sa.Column("code_digest", sa.String(length=64), nullable=True),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("requested_at", sa.DateTime(), nullable=False),
            sa.Column("challenged_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint("purpose IN ('mint', 'revoke')", name="check_challenge_purpose"),
            sa.CheckConstraint(
                "state IN ('requested', 'challenged', 'consumed', 'expired')",
                name="check_challenge_state",
            ),
            sa.UniqueConstraint("code_digest", name="uq_challenge_code_digest"),
        )
        op.create_index(
            "idx_challenges_agent_state", "auth_challenges", ["agent_id", "state"], unique=False
        )
        op.create_index(
            "idx_challenges_mailbox_active",
            "auth_challenges",
            ["agent_email", "state", "requested_at"],
            unique=False,
        )
        op.create_index(
            "idx_challenges_agent_purpose_state",
            "auth_challenges",
            ["agent_id", "purpose", "state"],
            unique=False,
        )

    if "auth_sessions" not in table_names:
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("agent_id", sa.Integer(), nullable=False),
            sa.Column("agent_email", sa.String(length=255), nullable=False),
            sa.Column("token_digest", sa.String(length=64), nullable=False),
            sa.Column("epoch", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(length=20), nullable=False),
            sa.Column("minted_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.CheckConstraint("state IN ('verified', 'revoked')", name="check_session_state"),
            sa.UniqueConstraint("token_digest", name="uq_session_token_digest"),
        )
        op.create_index(
            "idx_sessions_agent_state", "auth_sessions", ["agent_id", "state"], unique=False
        )

    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "auth_epoch" not in agent_cols:
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("auth_epoch", sa.Integer(), nullable=False, server_default="0")
            )


def downgrade() -> None:
    """Reverse: drop auth tables, remove agents.auth_epoch."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    agent_cols = {col["name"] for col in inspector.get_columns("agents")}
    if "auth_epoch" in agent_cols:
        with op.batch_alter_table("agents", schema=None) as batch_op:
            batch_op.drop_column("auth_epoch")

    table_names = set(inspector.get_table_names())
    if "auth_sessions" in table_names:
        op.drop_table("auth_sessions")
    if "auth_challenges" in table_names:
        op.drop_table("auth_challenges")