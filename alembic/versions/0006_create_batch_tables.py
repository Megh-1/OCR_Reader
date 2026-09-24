"""create batches and batch_documents

Batch processing (POST /documents/batch) needs somewhere durable to record what
was submitted and how far each document got, independent of Redis: RQ keeps job
state only for a while, and a batch's status must outlive that. The uploaded
bytes are stored on the row so a worker container needs nothing but the
database; they are nulled once the document is completed or has failed for good.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-24
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type_hint", sa.String(), nullable=True),
    )
    op.create_table(
        "batch_documents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.String(), sa.ForeignKey("batches.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.LargeBinary(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_batch_documents_batch_id", "batch_documents", ["batch_id"])


def downgrade() -> None:
    op.drop_index("ix_batch_documents_batch_id", table_name="batch_documents")
    op.drop_table("batch_documents")
    op.drop_table("batches")
