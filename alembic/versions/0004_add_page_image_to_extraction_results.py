"""add page_image to extraction_results

Module 9's Review UI is specified as letting the reviewer "see exactly what
the system saw" - which it could not do, because nothing persisted the page
image an extraction ran against. Nullable: rows written before this
migration have no image, and the UI degrades to no preview rather than
failing.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-19
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("extraction_results", sa.Column("page_image", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    op.drop_column("extraction_results", "page_image")
