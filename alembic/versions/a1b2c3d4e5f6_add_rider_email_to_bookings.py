"""add_rider_email_to_bookings

Revision ID: a1b2c3d4e5f6
Revises: 4ee4893205ee
Create Date: 2026-10-20 16:13:00.000000

Adds nullable rider_email column to bookings so the Stripe webhook can
trigger the Celery receipt-email task without needing a live Supabase call.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '4ee4893205ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add rider_email (nullable) to bookings."""
    op.add_column(
        'bookings',
        sa.Column('rider_email', sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    """Remove rider_email from bookings."""
    op.drop_column('bookings', 'rider_email')
