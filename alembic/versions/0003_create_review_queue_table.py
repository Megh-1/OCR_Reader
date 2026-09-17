"""create review_queue table

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "review_queue",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("resolution", JSONB(), nullable=True),
    )
    op.create_index("ix_review_queue_entry_type", "review_queue", ["entry_type"])
    op.create_index("ix_review_queue_status", "review_queue", ["status"])


def downgrade() -> None:
    op.drop_index("ix_review_queue_status", table_name="review_queue")
    op.drop_index("ix_review_queue_entry_type", table_name="review_queue")
    op.drop_table("review_queue")
