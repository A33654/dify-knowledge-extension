"""add dataset annotations

Revision ID: b3e4c5d6f708
Revises: a1f2e3d4c5b6
Create Date: 2026-10-26 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models.types

revision = "b3e4c5d6f708"
down_revision = "a1f2e3d4c5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dataset_annotations",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("tenant_id", models.types.StringUUID(), nullable=False),
        sa.Column("dataset_id", models.types.StringUUID(), nullable=False),
        sa.Column("question", models.types.LongText(), nullable=False),
        sa.Column("answer", models.types.LongText(), nullable=False),
        sa.Column("tags", models.types.AdjustedJSON(astext_type=sa.Text()), nullable=False),
        sa.Column("feedback_type", sa.String(255), nullable=True),
        sa.Column("created_by", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="dataset_annotation_pkey"),
    )
    op.create_index("dataset_annotation_tenant_idx", "dataset_annotations", ["tenant_id"])
    op.create_index("dataset_annotation_dataset_idx", "dataset_annotations", ["dataset_id"])
    op.create_index("dataset_annotation_dataset_created_idx", "dataset_annotations", ["dataset_id", "created_at"])


def downgrade() -> None:
    op.drop_index("dataset_annotation_dataset_created_idx", table_name="dataset_annotations")
    op.drop_index("dataset_annotation_dataset_idx", table_name="dataset_annotations")
    op.drop_index("dataset_annotation_tenant_idx", table_name="dataset_annotations")
    op.drop_table("dataset_annotations")
