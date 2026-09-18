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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_session
from app.models.template import TemplateRecord
from app.schemas.template import TemplateIn, TemplateOut

router = APIRouter()


def _latest_row(session: Session, doc_type: str) -> Optional[TemplateRecord]:
    """The highest-version row for doc_type, or None if it has no rows at all.

    Shared by the write path (_next_version) and the read path
    (get_latest_template) so there is exactly one "what's the latest
    version" query in this module, not two that could quietly drift apart.
    """
    return session.execute(
        select(TemplateRecord).where(TemplateRecord.doc_type == doc_type).order_by(TemplateRecord.version.desc()).limit(1)
    ).scalar_one_or_none()


def _next_version(session: Session, doc_type: str) -> int:
    latest = _latest_row(session, doc_type)
    return (latest.version if latest else 0) + 1


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
        "hints": payload.hints.model_dump(mode="json"),
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


# --- Module 4: read/query access layer -------------------------------------
#
# Read-only. Modules 5 and 6 import these functions directly and call them
# as plain Python - they are not routed through HTTP, so each one manages
# its own short-lived session rather than taking one as a parameter. The
# three GET endpoints below exist only for manual inspection/testing.


def get_latest_template(doc_type: str) -> Optional[TemplateOut]:
    """The highest-version template for doc_type, or None if doc_type has
    never been saved. None (not an exception, not an empty-but-truthy
    object) is the signal Module 5 depends on to refuse an unknown type."""
    with SessionLocal() as session:
        row = _latest_row(session, doc_type)
        return TemplateOut(**row.config) if row else None


def get_template_version(doc_type: str, version: int) -> Optional[TemplateOut]:
    """Exactly the requested version, even when a newer one exists for the
    same doc_type - never silently falls back to latest. Returns None if
    that exact (doc_type, version) pair doesn't exist."""
    with SessionLocal() as session:
        row = session.execute(
            select(TemplateRecord).where(
                TemplateRecord.doc_type == doc_type,
                TemplateRecord.version == version,
            )
        ).scalar_one_or_none()
        return TemplateOut(**row.config) if row else None


def list_known_doc_types() -> list[str]:
    """Every distinct doc_type currently in the table - one entry each,
    regardless of how many versions each one has saved."""
    with SessionLocal() as session:
        return list(session.execute(select(TemplateRecord.doc_type).distinct()).scalars().all())


@router.get("/templates", response_model=list[str])
def list_templates() -> list[str]:
    return list_known_doc_types()


@router.get("/templates/{doc_type}", response_model=TemplateOut)
def get_template(doc_type: str) -> TemplateOut:
    result = get_latest_template(doc_type)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No template found for doc_type '{doc_type}'")
    return result


@router.get("/templates/{doc_type}/versions/{version}", response_model=TemplateOut)
def get_template_by_version(doc_type: str, version: int) -> TemplateOut:
    result = get_template_version(doc_type, version)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No version {version} found for doc_type '{doc_type}'")
    return result
