"""add document versions and bind segments to versions

Revision ID: a1f2e3d4c5b6
Revises: b9d3f7a5c102
Create Date: 2026-10-25 10:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models as models

revision = "a1f2e3d4c5b6"
down_revision = "b9d3f7a5c102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_versions",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("tenant_id", models.types.StringUUID(), nullable=False),
        sa.Column("dataset_id", models.types.StringUUID(), nullable=False),
        sa.Column("document_id", models.types.StringUUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("business_key", sa.String(255), nullable=False),
        sa.Column("file_hash", sa.String(255), nullable=False),
        sa.Column("file_id", models.types.StringUUID(), nullable=True),
        sa.Column("file_name", sa.String(255), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("word_count", sa.Integer(), nullable=True),
        sa.Column("content", models.types.LongText(), nullable=True),
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id", name="document_version_pkey"),
    )
    op.create_index("document_version_document_id_idx", "document_versions", ["document_id"])
    op.create_index("document_version_tenant_idx", "document_versions", ["tenant_id"])
    op.create_index(
        "document_version_business_idx",
        "document_versions",
        ["tenant_id", "dataset_id", "business_key", "version"],
        unique=True,
    )

    op.add_column("document_segments", sa.Column("version_id", models.types.StringUUID(), nullable=True))
    op.create_index("document_segment_version_id_idx", "document_segments", ["version_id"])

    op.add_column("documents", sa.Column("current_version_id", models.types.StringUUID(), nullable=True))
    op.create_index("document_current_version_id_idx", "documents", ["current_version_id"])


def downgrade() -> None:
    op.drop_index("document_current_version_id_idx", table_name="documents")
    op.drop_column("documents", "current_version_id")

    op.drop_index("document_segment_version_id_idx", table_name="document_segments")
    op.drop_column("document_segments", "version_id")

    op.drop_index("document_version_business_idx", table_name="document_versions")
    op.drop_index("document_version_tenant_idx", table_name="document_versions")
    op.drop_index("document_version_document_id_idx", table_name="document_versions")
    op.drop_table("document_versions")

