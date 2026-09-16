"""
Module 3 (backend half) + the minimal slice of Module 4 it depends on.

Module 3's job here is just persistence: validate an incoming template
config and save it. Module 4's job is being "the library of everything the
system currently knows how to read" - modeled here as the read-side queries
(current max version) that persistence needs. Anchor marking / the canvas UI
is not part of this file.

Write-once, by construction: the only public write function is
create_template_version(), which always INSERTs a new row. There is no
update/delete function anywhere in this module - past extractions stay
reproducible against the exact template version that produced them.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import get_session
from app.models.template import TemplateRecord
from app.schemas.template import TemplateIn, TemplateOut

router = APIRouter()


def _next_version(session: Session, doc_type: str) -> int:
    current_max = session.execute(
        select(func.max(TemplateRecord.version)).where(TemplateRecord.doc_type == doc_type)
    ).scalar()
    return (current_max or 0) + 1


def create_template_version(session: Session, payload: TemplateIn) -> TemplateOut:
    """
    Validates and inserts a new template version. Concurrency safety: an
    advisory lock keyed on doc_type is held for the rest of this DB
    transaction, so two concurrent saves for the same new doc_type are
    serialized at the database level instead of racing to compute the same
    "next version" - the second save simply waits, then sees the first
    save's row and correctly computes version+1. The lock is transaction-
    scoped (pg_advisory_xact_lock) and releases automatically on commit.
    """
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:doc_type))"), {"doc_type": payload.template_id})

    next_version = _next_version(session, payload.template_id)
    created_at = date.today()

    config = {
        "template_id": payload.template_id,
        "version": next_version,
        "anchors": [a.model_dump(mode="json") for a in payload.anchors],
        "fields": [f.model_dump(mode="json") for f in payload.fields],
        "created_at": created_at.isoformat(),
    }

    record = TemplateRecord(
        doc_type=payload.template_id,
        version=next_version,
        config=config,
        created_at=created_at,
    )
    session.add(record)
    session.commit()

    return TemplateOut(**config)


@router.post("/templates", response_model=TemplateOut, status_code=201)
def post_template(payload: TemplateIn, session: Session = Depends(get_session)) -> TemplateOut:
    return create_template_version(session, payload)
