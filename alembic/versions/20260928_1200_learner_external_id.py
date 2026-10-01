"""learner.external_id — the learner's LMS user id

Revision ID: 7c3e9a1f5d20
Revises: 2b7d41c9ae03
Create Date: 2026-09-28 12:00:00.000000+00:00

Additive and nullable: learners registered before this release have no LMS id,
and old registration clients that never send one keep working. The unique
constraint's index also serves the webhook lookup on (organization_id, external_id).
"""
from alembic import op
import sqlalchemy as sa

revision = '7c3e9a1f5d20'
down_revision = '2b7d41c9ae03'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('learner', sa.Column('external_id', sa.BigInteger(), nullable=True))
    op.create_unique_constraint(
        'uq_learner_org_external_id', 'learner', ['organization_id', 'external_id']
    )


def downgrade() -> None:
    # Drops every stored LMS id; re-registering learners with external_id restores them.
    op.drop_constraint('uq_learner_org_external_id', 'learner', type_='unique')
    op.drop_column('learner', 'external_id')
