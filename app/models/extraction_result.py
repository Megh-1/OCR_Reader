from datetime import datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ExtractionResultRecord(Base):
    """
    A standalone log entry for one extraction run. Unlike TemplateRecord,
    this has no versioning semantics of its own - no uniqueness constraint,
    nothing ever supersedes a row here. It just records what happened, when,
    against which template version.
    """

    __tablename__ = "extraction_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    overall_status: Mapped[str] = mapped_column(String, nullable=False)
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
