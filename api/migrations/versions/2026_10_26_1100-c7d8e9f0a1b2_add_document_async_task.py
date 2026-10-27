"""add document async task

Revision ID: c7d8e9f0a1b2
Revises: b3e4c5d6f708
Create Date: 2026-10-26 17:20:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models as models

revision = "c7d8e9f0a1b2"
down_revision = "b3e4c5d6f708"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_async_task",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("tenant_id", models.types.StringUUID(), nullable=False),
        sa.Column("dataset_id", models.types.StringUUID(), nullable=False),
        sa.Column("trace_id", sa.String(128), nullable=False),
        sa.Column("file_id", models.types.StringUUID(), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("created_by", models.types.StringUUID(), nullable=False),
        sa.Column("payload", models.types.AdjustedJSON(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("document_id", models.types.StringUUID(), nullable=True),
        sa.Column("version_id", models.types.StringUUID(), nullable=True),
        sa.Column("batch", sa.String(255), nullable=True),
        sa.Column("requires_indexing", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", models.types.LongText(), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id", name="document_async_task_pkey"),
        sa.UniqueConstraint(
            "tenant_id",
            "dataset_id",
            "trace_id",
            name="document_async_task_tenant_dataset_trace_uq",
        ),
    )
    op.create_index("document_async_task_status_idx", "document_async_task", ["status", "created_at"])
    op.create_index("document_async_task_dataset_idx", "document_async_task", ["tenant_id", "dataset_id"])


def downgrade() -> None:
    op.drop_index("document_async_task_dataset_idx", table_name="document_async_task")
    op.drop_index("document_async_task_status_idx", table_name="document_async_task")
    op.drop_table("document_async_task")
