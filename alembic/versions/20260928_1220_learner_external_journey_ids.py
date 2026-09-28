"""learner.external_journey_ids — the LMS journeys the learner is enrolled in

Revision ID: 9e1b5c3d7a42
Revises: 4a8d2e6b91c7
Create Date: 2026-09-28 12:20:00.000000+00:00

Additive: the server default gives existing learners an empty list, and code from
before this release never reads or writes the column.
"""

from alembic import op
import sqlalchemy as sa

revision = "9e1b5c3d7a42"
down_revision = "4a8d2e6b91c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "learner",
        sa.Column(
            "external_journey_ids",
            sa.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    # Drops the synced enrollments; the next LMS journey webhook per learner restores them.
    op.drop_column("learner", "external_journey_ids")
