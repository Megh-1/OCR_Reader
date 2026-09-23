"""add is_archived to templates

Templates are append-only, so retiring one cannot be a DELETE. This adds a
lifecycle flag instead: an archived doc_type is skipped when Module 5 scans
for a match, while every version row stays intact so any past extraction
remains reproducible against the exact template that produced it.

server_default false, so rows written before this migration - and rows
written by a process still running the old model - are unarchived.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "templates",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("templates", "is_archived")
