"""add thread_close_votes for vote-to-close (#104)

Revision ID: 575a93a1f627
Revises: 078a97a100f2
Create Date: 2026-09-06 11:45:35.439264

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '575a93a1f627'
down_revision: Union[str, Sequence[str], None] = '078a97a100f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('thread_close_votes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('root_post_id', sa.Integer(), nullable=False),
    sa.Column('voter', sa.String(length=255), nullable=False),
    sa.Column('as_of_event_kind', sa.String(length=16), nullable=False),
    sa.Column('as_of_event_id', sa.Integer(), nullable=False),
    sa.Column('as_of_event_at', sa.DateTime(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint("as_of_event_kind IN ('comment', 'post')", name='check_close_vote_event_kind'),
    sa.ForeignKeyConstraint(['root_post_id'], ['posts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('root_post_id', 'voter', name='uq_close_vote_thread_voter')
    )
    op.create_index('idx_close_votes_root_post_id', 'thread_close_votes', ['root_post_id'], unique=False)
    # NOTE: autogenerate also proposed dropping and recreating the
    # comments.in_reply_to foreign key. That is SQLite reflection noise for an
    # unnamed constraint already migrated to SET NULL in b3c5d7e9f1a2 — the
    # generated op passes name=None, which fails on Postgres. Removed
    # deliberately; this migration adds one table and nothing else.


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_close_votes_root_post_id', table_name='thread_close_votes')
    op.drop_table('thread_close_votes')
