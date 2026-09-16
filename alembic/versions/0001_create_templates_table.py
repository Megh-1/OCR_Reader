"""create templates table

Revision ID: 0001
Revises:
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("doc_type", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("created_at", sa.Date(), nullable=False),
        sa.UniqueConstraint("doc_type", "version", name="uq_templates_doc_type_version"),
    )
    op.create_index("ix_templates_doc_type", "templates", ["doc_type"])


def downgrade() -> None:
    op.drop_index("ix_templates_doc_type", table_name="templates")
    op.drop_table("templates")
