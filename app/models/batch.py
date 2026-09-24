from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BatchRecord(Base):
    """One POST /documents/batch: the documents submitted together, and the hint they share."""

    __tablename__ = "batches"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # uuid4 hex, handed back to the caller
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type_hint: Mapped[Optional[str]] = mapped_column(String, nullable=True)


class BatchDocumentRecord(Base):
    """
    One document in a batch, and everything a worker needs to process it.

    The uploaded bytes live here rather than in Redis or on a shared disk so an
    API container and a worker container need share nothing but the database.
    They are dropped once the document reaches a terminal state.

    `result` grows page by page while the document is processed, so a retry
    resumes at the first unfinished page instead of redoing pages that already
    wrote extraction results and review-queue entries.
    """

    __tablename__ = "batch_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[str] = mapped_column(String, ForeignKey("batches.id"), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    # queued | processing | retrying | completed | failed
    status: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
