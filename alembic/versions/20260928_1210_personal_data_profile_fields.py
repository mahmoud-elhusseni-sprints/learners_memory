"""learner_personal_data: profile fields synced from the LMS

Revision ID: 4a8d2e6b91c7
Revises: 7c3e9a1f5d20
Create Date: 2026-09-28 12:10:00.000000+00:00

Additive, all nullable: existing rows need no backfill, and code from before this
release never reads or writes these columns.
"""
from alembic import op
import sqlalchemy as sa

revision = '4a8d2e6b91c7'
down_revision = '7c3e9a1f5d20'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('learner_personal_data', sa.Column('phone_country_code', sa.String(length=8), nullable=True))
    op.add_column('learner_personal_data', sa.Column('bio', sa.Text(), nullable=True))
    op.add_column('learner_personal_data', sa.Column('avatar_url', sa.Text(), nullable=True))
    op.add_column('learner_personal_data', sa.Column('timezone', sa.String(length=64), nullable=True))
    op.add_column('learner_personal_data', sa.Column('preferred_language', sa.String(length=16), nullable=True))
    op.add_column('learner_personal_data', sa.Column('job_preference', sa.String(length=32), nullable=True))
    op.add_column('learner_personal_data', sa.Column('github_url', sa.Text(), nullable=True))
    op.add_column('learner_personal_data', sa.Column('linkedin_url', sa.Text(), nullable=True))
    op.add_column('learner_personal_data', sa.Column('cv_url', sa.Text(), nullable=True))
    op.add_column('learner_personal_data', sa.Column('github_id', sa.String(length=255), nullable=True))
    op.add_column('learner_personal_data', sa.Column('linkedin_id', sa.String(length=255), nullable=True))
    op.add_column('learner_personal_data', sa.Column('external_updated_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    # Drops the synced values; the next LMS profile webhook per learner restores them.
    op.drop_column('learner_personal_data', 'external_updated_at')
    op.drop_column('learner_personal_data', 'linkedin_id')
    op.drop_column('learner_personal_data', 'github_id')
    op.drop_column('learner_personal_data', 'cv_url')
    op.drop_column('learner_personal_data', 'linkedin_url')
    op.drop_column('learner_personal_data', 'github_url')
    op.drop_column('learner_personal_data', 'job_preference')
    op.drop_column('learner_personal_data', 'preferred_language')
    op.drop_column('learner_personal_data', 'timezone')
    op.drop_column('learner_personal_data', 'avatar_url')
    op.drop_column('learner_personal_data', 'bio')
    op.drop_column('learner_personal_data', 'phone_country_code')
