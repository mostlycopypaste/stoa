"""merge heads a1b2c3d4e5f6 and b3c5d7e9f1a2

Revision ID: 078a97a100f2
Revises: a1b2c3d4e5f6, b3c5d7e9f1a2
Create Date: 2026-08-14 17:20:53.645829

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '078a97a100f2'
down_revision: Union[str, Sequence[str], None] = ('a1b2c3d4e5f6', 'b3c5d7e9f1a2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
