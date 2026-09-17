"""create extraction_results table

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "extraction_results",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("overall_status", sa.String(), nullable=False),
        sa.Column("fields", JSONB(), nullable=False),
    )
    op.create_index("ix_extraction_results_template_id", "extraction_results", ["template_id"])


def downgrade() -> None:
    op.drop_index("ix_extraction_results_template_id", table_name="extraction_results")
    op.drop_table("extraction_results")
