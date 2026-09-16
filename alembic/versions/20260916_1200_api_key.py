"""api_key table

Revision ID: 2b7d41c9ae03
Revises: 956f9c43fbb3
Create Date: 2026-09-16 12:00:00.000000+00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '2b7d41c9ae03'
down_revision = '956f9c43fbb3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('api_key',
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('key_hash', sa.String(length=64), nullable=False),
    sa.Column('scopes', sa.ARRAY(sa.Text()), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('organization_id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_api_key_active'), 'api_key', ['active'], unique=False)
    op.create_index(op.f('ix_api_key_organization_id'), 'api_key', ['organization_id'], unique=False)
    op.create_index(op.f('ix_api_key_prefix'), 'api_key', ['prefix'], unique=True)


def downgrade() -> None:
    op.drop_index(op.f('ix_api_key_prefix'), table_name='api_key')
    op.drop_index(op.f('ix_api_key_organization_id'), table_name='api_key')
    op.drop_index(op.f('ix_api_key_active'), table_name='api_key')
    op.drop_table('api_key')
