from sqlalchemy import Boolean, Date, Integer, String, UniqueConstraint, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class TemplateRecord(Base):
    """
    A single saved version of a document type's template. Rows are
    write-once: a new save is always a new row (new_version = max+1), never
    an UPDATE of an existing one — see app/modules/template_store.py, whose
    public API has no update/delete path at all, only inserts. This is what
    lets a past extraction stay reproducible against the exact template
    version that actually ran.
    """

    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("doc_type", "version", name="uq_templates_doc_type_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String, nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[Date] = mapped_column(Date, nullable=False)
    # Lifecycle flag, not template content: `config` is never touched by
    # archiving. A doc_type counts as archived when its LATEST version is, so
    # saving a new version brings a retired type back into use.
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
