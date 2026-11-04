"""create the engine-selection tables

Which reader engines read a template is decided from measurements (app/modules/engine_selection): scorecards per
(template, engine), how pairs of engines agreed, which engines were promoted into a template's chain, a log of every chain change
with its numbers, and the values people settled, kept as labels so the next measurement can use them.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "engine_scorecards",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("engine", sa.String(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("documents", sa.Integer(), nullable=False),
        sa.Column("fields", sa.Integer(), nullable=False),
        sa.Column("right", sa.Integer(), nullable=False),
        sa.Column("wrong_auto", sa.Integer(), nullable=False),
        sa.Column("queued_fields", sa.Integer(), nullable=False),
        sa.Column("queued_right", sa.Integer(), nullable=False),
        sa.Column("docs_per_min", sa.Float(), nullable=False),
        sa.Column("by_kind", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_engine_scorecards_template_id", "engine_scorecards", ["template_id"])
    op.create_index("ix_engine_scorecards_engine", "engine_scorecards", ["engine"])

    op.create_table(
        "engine_pairs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("engine_a", sa.String(), nullable=False),
        sa.Column("engine_b", sa.String(), nullable=False),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("fields", sa.Integer(), nullable=False),
        sa.Column("agreed", sa.Integer(), nullable=False),
        sa.Column("agreed_right", sa.Integer(), nullable=False),
    )
    op.create_index("ix_engine_pairs_template_id", "engine_pairs", ["template_id"])

    op.create_table(
        "engine_promotions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("engine", sa.String(), nullable=False),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("corpus_id", sa.String(), nullable=False),
        sa.Column("numbers", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("template_id", "engine", name="uq_engine_promotion"),
    )
    op.create_index("ix_engine_promotions_template_id", "engine_promotions", ["template_id"])

    op.create_table(
        "engine_chain_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("chain", postgresql.JSONB(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("numbers", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_engine_chain_log_template_id", "engine_chain_log", ["template_id"])

    op.create_table(
        "labelled_examples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("template_version", sa.Integer(), nullable=False),
        sa.Column("extraction_result_id", sa.Integer(), nullable=False),
        sa.Column("field_id", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("machine_value", sa.String(), nullable=True),
        sa.Column("engine", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("extraction_result_id", "field_id", name="uq_labelled_field"),
    )
    op.create_index("ix_labelled_examples_template_id", "labelled_examples", ["template_id"])
    op.create_index("ix_labelled_examples_extraction_result_id", "labelled_examples", ["extraction_result_id"])


def downgrade() -> None:
    for table in ("labelled_examples", "engine_chain_log", "engine_promotions", "engine_pairs", "engine_scorecards"):
        op.drop_table(table)
