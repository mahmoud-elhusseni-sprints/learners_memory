"""learning_journey / journey_step: LMS journey enrollment and program progress

Revision ID: 9e1b5c3a7d42
Revises: 4a8d2e6b91c7
Create Date: 2026-09-30 12:00:00.000000+00:00

Additive: new columns are nullable or carry a server default, so existing rows
and code from before this release are unaffected. Three columns are relaxed to
nullable because the LMS does not send them (journey name, step title) or they
cannot be derived from it (step status).

Upgrade adds a 0..1 check on the existing learning_journey.progress and fails if
a row is out of range; nothing wrote that column before this release.

Downgrade restores NOT NULL on the relaxed columns, so it first fills their
nulls: step status with its old default 'planned', names and titles from the
LMS slug or id. Those filled values are not reverted by a later re-upgrade.
"""
from alembic import op
import sqlalchemy as sa

revision = '9e1b5c3a7d42'
down_revision = '4a8d2e6b91c7'
branch_labels = None
depends_on = None

_COUNTERS = (
    'files_completed', 'videos_completed', 'text_lessons_completed',
    'live_sessions_completed', 'recorded_sessions_completed', 'codelabs_completed',
    'quizzes_completed', 'tasks_completed', 'regular_projects_completed',
    'final_projects_completed', 'peer_reviews_completed', 'ai_interviews_completed',
)


def upgrade() -> None:
    op.add_column('learning_journey', sa.Column('external_id', sa.BigInteger(), nullable=True))
    op.add_column('learning_journey', sa.Column('slug', sa.String(length=255), nullable=True))
    op.add_column('learning_journey', sa.Column('public_url', sa.Text(), nullable=True))
    op.add_column('learning_journey', sa.Column('blocked', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('learning_journey', sa.Column('manual_added', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('learning_journey', sa.Column('started_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('learning_journey', sa.Column('graduated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('learning_journey', sa.Column('enrollment_updated_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('learning_journey', sa.Column('progress_updated_at', sa.DateTime(timezone=True), nullable=True))
    op.alter_column('learning_journey', 'name', existing_type=sa.String(length=255), nullable=True)
    op.create_unique_constraint(
        'uq_learning_journey_learner_external_id', 'learning_journey', ['learner_id', 'external_id']
    )
    op.create_check_constraint(
        'ck_learning_journey_progress_fraction', 'learning_journey', 'progress >= 0 AND progress <= 1'
    )

    op.add_column('journey_step', sa.Column('external_id', sa.BigInteger(), nullable=True))
    for counter in _COUNTERS:
        op.add_column('journey_step', sa.Column(counter, sa.Integer(), server_default='0', nullable=False))
    op.alter_column('journey_step', 'title', existing_type=sa.String(length=255), nullable=True)
    op.alter_column('journey_step', 'status', existing_type=sa.String(length=32), nullable=True)
    op.create_unique_constraint(
        'uq_journey_step_journey_external_id', 'journey_step', ['journey_id', 'external_id']
    )
    op.create_check_constraint(
        'ck_journey_step_counters_non_negative', 'journey_step',
        ' AND '.join(f'{counter} >= 0' for counter in _COUNTERS),
    )


def downgrade() -> None:
    op.drop_constraint('ck_journey_step_counters_non_negative', 'journey_step', type_='check')
    op.drop_constraint('uq_journey_step_journey_external_id', 'journey_step', type_='unique')
    op.execute("UPDATE journey_step SET status = 'planned' WHERE status IS NULL")
    op.execute(
        "UPDATE journey_step SET title = coalesce('LMS program ' || external_id, 'Untitled step') "
        "WHERE title IS NULL"
    )
    op.alter_column('journey_step', 'status', existing_type=sa.String(length=32), nullable=False)
    op.alter_column('journey_step', 'title', existing_type=sa.String(length=255), nullable=False)
    for counter in reversed(_COUNTERS):
        op.drop_column('journey_step', counter)
    op.drop_column('journey_step', 'external_id')

    op.drop_constraint('ck_learning_journey_progress_fraction', 'learning_journey', type_='check')
    op.drop_constraint('uq_learning_journey_learner_external_id', 'learning_journey', type_='unique')
    op.execute(
        "UPDATE learning_journey "
        "SET name = coalesce(slug, 'LMS journey ' || external_id, 'Untitled journey') "
        "WHERE name IS NULL"
    )
    op.alter_column('learning_journey', 'name', existing_type=sa.String(length=255), nullable=False)
    op.drop_column('learning_journey', 'progress_updated_at')
    op.drop_column('learning_journey', 'enrollment_updated_at')
    op.drop_column('learning_journey', 'graduated_at')
    op.drop_column('learning_journey', 'started_at')
    op.drop_column('learning_journey', 'manual_added')
    op.drop_column('learning_journey', 'blocked')
    op.drop_column('learning_journey', 'public_url')
    op.drop_column('learning_journey', 'slug')
    op.drop_column('learning_journey', 'external_id')
