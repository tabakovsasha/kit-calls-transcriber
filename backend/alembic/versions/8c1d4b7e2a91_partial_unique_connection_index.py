"""scope connection uniqueness to live rows

Deletes of voximplant_connections are soft (deleted_at is set and the token
ciphertext is scrubbed). The original table-wide UniqueConstraint therefore let
a dead row keep reserving (owner_user_id, api_host, domain) forever: the service
layer check skips soft-deleted rows, so a create-after-delete passed validation
and then failed in the database as an unhandled IntegrityError.

Replacing the constraint with a partial unique index aligns the database with
the intended rule: uniqueness applies only to connections that still exist.

Revision ID: 8c1d4b7e2a91
Revises: 45f3346af8db
Create Date: 2026-08-31 02:05:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '8c1d4b7e2a91'
down_revision: Union[str, None] = '45f3346af8db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = 'uq_connection_owner_host_domain'
TABLE_NAME = 'voximplant_connections'


def upgrade() -> None:
    op.drop_constraint(INDEX_NAME, TABLE_NAME, type_='unique')
    op.create_index(
        INDEX_NAME,
        TABLE_NAME,
        ['owner_user_id', 'api_host', 'domain'],
        unique=True,
        postgresql_where=sa.text('deleted_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name=TABLE_NAME)
    op.create_unique_constraint(
        INDEX_NAME, TABLE_NAME, ['owner_user_id', 'api_host', 'domain']
    )
