from datetime import datetime

from typing import Optional

from sqlalchemy import Boolean, DateTime, Integer, LargeBinary, String, true
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
    # False when the page was read by the page engines with no template that matched it (template_id is then the base schema's id).
    template_matched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # A review-sized JPEG render of the page this ran against, so Module 9's
    # reviewer sees the actual document. Nullable - rows predating this
    # column, and any caller that doesn't supply one, simply have no preview.
    page_image: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
