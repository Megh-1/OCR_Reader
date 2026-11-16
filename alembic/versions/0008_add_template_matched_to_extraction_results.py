"""record whether a result's template matched the page

A page read by the page engines with no matching template is still stored (against the base air_waybill schema, whose rules judge it), and its
result says so: template_matched = false. Every row that predates this was read through a matched template.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("extraction_results", sa.Column("template_matched", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    op.drop_column("extraction_results", "template_matched")
