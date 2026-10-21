"""test migration

Revision ID: c93c11fd2541
Revises: 2b0114747f9e
Create Date: 2026-10-20 10:18:34.983673

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c93c11fd2541'
down_revision: Union[str, Sequence[str], None] = '2b0114747f9e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'bookings',
        sa.Column('test_column', sa.String(length=50), nullable=True)
    )


def downgrade() -> None:
    op.drop_column('bookings', 'test_column')###
