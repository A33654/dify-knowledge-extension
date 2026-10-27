"""extend document async task retry

Adds the retry bookkeeping columns used by the asynchronous document upload
worker. The ``dead_letter`` status is a new ``DocumentAsyncTaskStatus`` member;
because the column is persisted through ``models.types.EnumText`` (a VARCHAR
type decorator) it needs no DDL change, so this migration only touches columns.

Revision ID: a1b2c3d4e5f6
Revises: c7d8e9f0a1b2
Create Date: 2026-10-26 21:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models as models

revision = "a1b2c3d4e5f6"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_async_task", sa.Column("last_error_code", sa.String(255), nullable=True))
    op.add_column("document_async_task", sa.Column("next_attempt_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_async_task", "next_attempt_at")
    op.drop_column("document_async_task", "last_error_code")
