from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EngineScorecardRecord(Base):
    """One measurement of one engine on one template's labelled documents (scripts/evaluate_reader.py --save). Append-only: the
    LATEST row per (template_id, engine) is the engine's current standing, older rows are the history of how it moved."""

    __tablename__ = "engine_scorecards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String, nullable=False, index=True)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)  # which documents: two scores compare only on the same corpus
    documents: Mapped[int] = mapped_column(Integer, nullable=False)
    fields: Mapped[int] = mapped_column(Integer, nullable=False)
    right: Mapped[int] = mapped_column(Integer, nullable=False)
    wrong_auto: Mapped[int] = mapped_column(Integer, nullable=False)  # wrong values that would have shipped unchecked
    queued_fields: Mapped[int] = mapped_column(Integer, nullable=False)  # fields a person would see (spot_check or review)
    queued_right: Mapped[int] = mapped_column(Integer, nullable=False)
    docs_per_min: Mapped[float] = mapped_column(Float, nullable=False)
    by_kind: Mapped[dict] = mapped_column(JSONB, nullable=False)


class EnginePairRecord(Base):
    """How two engines agreed on one corpus: the evidence for letting agreement raise a value's trust."""

    __tablename__ = "engine_pairs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    engine_a: Mapped[str] = mapped_column(String, nullable=False)  # a < b, so a pair has one spelling
    engine_b: Mapped[str] = mapped_column(String, nullable=False)
    measured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    fields: Mapped[int] = mapped_column(Integer, nullable=False)
    agreed: Mapped[int] = mapped_column(Integer, nullable=False)  # same non-empty value
    agreed_right: Mapped[int] = mapped_column(Integer, nullable=False)


class EnginePromotionRecord(Base):
    """An engine admitted into a template's chain because it beat the incumbent on a shared corpus. Without a row here an engine is
    only a candidate, however it scored."""

    __tablename__ = "engine_promotions"
    __table_args__ = (UniqueConstraint("template_id", "engine", name="uq_engine_promotion"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    engine: Mapped[str] = mapped_column(String, nullable=False)
    promoted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    corpus_id: Mapped[str] = mapped_column(String, nullable=False)
    numbers: Mapped[dict] = mapped_column(JSONB, nullable=False)  # the candidate's and the incumbent's figures behind the decision


class EngineChainLogRecord(Base):
    """Every change of a template's chain, with the numbers that decided it."""

    __tablename__ = "engine_chain_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    chain: Mapped[list] = mapped_column(JSONB, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)  # measured | cold_start | override
    numbers: Mapped[dict] = mapped_column(JSONB, nullable=False)


class LabelledExampleRecord(Base):
    """A value a person settled, kept as a label: the result it belongs to (whose stored page image is the document) and what the
    field truly says. These are the documents the next scorecard can be measured on (scripts/export_labelled.py)."""

    __tablename__ = "labelled_examples"
    __table_args__ = (UniqueConstraint("extraction_result_id", "field_id", name="uq_labelled_field"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_result_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    field_id: Mapped[str] = mapped_column(String, nullable=False)
    value: Mapped[str] = mapped_column(String, nullable=False)
    machine_value: Mapped[Optional[str]] = mapped_column(String, nullable=True)  # what the engine had read, to see what the person changed
    engine: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
