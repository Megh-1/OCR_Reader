from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ReviewQueueRecord(Base):
    """
    Module 9's backlog. entry_type distinguishes "a field needs a human to
    confirm its value" from "no template matched, one needs to be created" -
    payload/resolution shapes differ accordingly (see app/schemas/review_queue.py).
    """

    __tablename__ = "review_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    resolution: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
