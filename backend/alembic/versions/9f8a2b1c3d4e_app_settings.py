"""app_settings table for Whisper model and performance profile persistence

Revision ID: 9f8a2b1c3d4e
Revises: 8c1d4b7e2a91
Create Date: 2026-08-31 02:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '9f8a2b1c3d4e'
down_revision: Union[str, None] = '8c1d4b7e2a91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('key', sa.String(length=128), nullable=False),
        sa.Column('value', JSONB, nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )
    op.create_index('ix_app_settings_key', 'app_settings', ['key'])

    # Insert defaults: base model, moderate profile
    op.execute("""
        INSERT INTO app_settings (key, value) VALUES
        ('whisper.default_model', '"base"'),
        ('whisper.performance_profile', '"moderate"')
    """)


def downgrade() -> None:
    op.drop_index('ix_app_settings_key', table_name='app_settings')
    op.drop_table('app_settings')
