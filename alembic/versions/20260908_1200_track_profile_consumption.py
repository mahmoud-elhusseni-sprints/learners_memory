"""track profile contribution consumption

Revision ID: 2ac8955c448c
Revises: 956f9c43fbb3
Create Date: 2026-09-08 12:00:00+00:00
"""

import sqlalchemy as sa

from alembic import op

revision = "2ac8955c448c"
down_revision = "956f9c43fbb3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "card_contribution",
        sa.Column(
            "profile_consumed_card_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("card_contribution", "profile_consumed_card_updated_at")
