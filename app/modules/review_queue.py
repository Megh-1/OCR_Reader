"""
Module 9 backend - review queue + resolution.

Standalone functions Module 5/8 COULD call to enqueue a rejection or a
flagged field for human review - nothing calls them yet. That wiring is a
separate task; this module's job is just: enqueue, resolve, list.

resolve_field_review does a REAL write into Module 8's extraction_results
table (not just marking the queue row done), inside the same transaction
as marking the queue entry resolved, so a failure partway through can't
leave the queue "resolved" while the underlying result is still wrong, or
the result silently corrected while the queue row is stuck pending.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from app.db import SessionLocal
from app.models.batch import BatchDocumentRecord, BatchRecord
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord
from app.modules.template_store import get_latest_template
from app.schemas.review_queue import (
    FieldReviewPayload,
    FieldReviewResolution,
    NewTemplateNeededPayload,
    NewTemplateNeededResolution,
    ReviewQueueEntry,
    ReviewResolutionRequest,
    SpotCheckPayload,
    SpotCheckResolution,
)

router = APIRouter()


class ReviewQueueError(Exception):
    """Base class for this module's own rejections."""


class ReviewEntryNotFoundError(ReviewQueueError):
    pass


class ReviewTypeMismatchError(ReviewQueueError):
    pass


class TemplateNotFoundError(ReviewQueueError):
    pass


def _enqueue(entry_type: str, payload: dict) -> int:
    with SessionLocal() as session:
        record = ReviewQueueRecord(
            entry_type=entry_type,
            status="pending",
            created_at=datetime.now(timezone.utc),
            resolved_at=None,
            payload=payload,
            resolution=None,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id


def enqueue_field_review(
    extraction_result_id: int, field_id: str, original_value: str, reasons: list[str], confidence: Optional[float] = None
) -> int:
    # Constructing this model is the fail-fast validation: a malformed call
    # (wrong types, missing pieces) errors here, not as a mystery later.
    payload = FieldReviewPayload(
        extraction_result_id=extraction_result_id, field_id=field_id, original_value=original_value, reasons=reasons,
        confidence=confidence,
    )
    return _enqueue("field_review", payload.model_dump(mode="json"))


def enqueue_spot_check(
    extraction_result_id: int, field_id: str, original_value: str, confidence: float, why: str, audit: bool = False,
    reader: Optional[str] = None,
) -> int:
    """A field that passed validation but did not earn `auto`. It is not flagged - the record's
    overall_status is unaffected - it just gets a person's glance. `audit` marks one a trusted field would
    have skipped, picked at random so the trust stays measured."""
    payload = SpotCheckPayload(
        extraction_result_id=extraction_result_id, field_id=field_id, original_value=original_value,
        confidence=confidence, why=why, audit=audit, reader=reader,
    )
    return _enqueue("spot_check", payload.model_dump(mode="json"))


def enqueue_new_template_needed(attempted_type_hint: Optional[str], rejection_reason: str) -> int:
    payload = NewTemplateNeededPayload(attempted_type_hint=attempted_type_hint, rejection_reason=rejection_reason)
    return _enqueue("new_template_needed", payload.model_dump(mode="json"))


VALUE_ENTRY_TYPES = ("field_review", "spot_check")  # entries a person resolves by confirming a value


def resolve_field_review(queue_id: int, confirmed_value: str) -> None:
    """Resolves a field_review OR a spot_check entry: both are settled by confirming (or correcting)
    the field's value, and both write that value into the extraction result."""
    with SessionLocal() as session:
        row = session.get(ReviewQueueRecord, queue_id)
        if row is None:
            raise ReviewEntryNotFoundError(f"No review queue entry with id {queue_id}")
        if row.entry_type not in VALUE_ENTRY_TYPES:
            raise ReviewTypeMismatchError(
                f"queue id {queue_id} is entry_type={row.entry_type!r}, not one of {VALUE_ENTRY_TYPES}"
            )
        apply_confirmed_value(session, row, confirmed_value, via="single")
        session.commit()  # one transaction: both writes land together or neither does


def apply_confirmed_value(session, row: ReviewQueueRecord, confirmed_value: str, via: str) -> None:
    """The write shared by a person's single resolution and a bulk accept. The caller commits."""
    payload_model = FieldReviewPayload if row.entry_type == "field_review" else SpotCheckPayload
    payload = payload_model.model_validate(row.payload)

    result_row = session.get(ExtractionResultRecord, payload.extraction_result_id)
    if result_row is None:
        raise ReviewQueueError(f"extraction result {payload.extraction_result_id} not found")
    if payload.field_id not in result_row.fields:
        raise ReviewQueueError(
            f"field {payload.field_id!r} not found in extraction result {payload.extraction_result_id}"
        )

    # Real write into Module 8's data - reassign the whole dict so
    # SQLAlchemy detects the JSONB column actually changed.
    updated_fields = dict(result_row.fields)
    updated_field = dict(updated_fields[payload.field_id])
    updated_field["value"] = confirmed_value
    updated_field["needs_review"] = False
    updated_field["verified_by"] = "person" if via == "single" else "bulk"
    updated_fields[payload.field_id] = updated_field
    result_row.fields = updated_fields

    # Re-check every field in this result, not just the one just fixed -
    # only flip the result's own status back once nothing in it is still
    # flagged. Same transaction as the write above: both land together
    # or neither does.
    if not any(f["needs_review"] for f in updated_fields.values()):
        result_row.overall_status = "complete"

    row.status = "resolved"
    row.resolved_at = datetime.now(timezone.utc)
    resolution_model = FieldReviewResolution if row.entry_type == "field_review" else SpotCheckResolution
    row.resolution = resolution_model(confirmed_value=confirmed_value, via=via).model_dump(mode="json")


def resolve_new_template_needed(queue_id: int, created_template_id: str) -> None:
    with SessionLocal() as session:
        row = session.get(ReviewQueueRecord, queue_id)
        if row is None:
            raise ReviewEntryNotFoundError(f"No review queue entry with id {queue_id}")
        if row.entry_type != "new_template_needed":
            raise ReviewTypeMismatchError(
                f"queue id {queue_id} is entry_type={row.entry_type!r}, not 'new_template_needed'"
            )

        # Never trust the string blindly - it must genuinely exist.
        if get_latest_template(created_template_id) is None:
            raise TemplateNotFoundError(f"template_id {created_template_id!r} does not exist in the template store")

        row.status = "resolved"
        row.resolved_at = datetime.now(timezone.utc)
        row.resolution = NewTemplateNeededResolution(created_template_id=created_template_id).model_dump(mode="json")

        session.commit()


def _list_entries(status: Optional[str], entry_type: Optional[str]) -> list[ReviewQueueEntry]:
    with SessionLocal() as session:
        stmt = select(ReviewQueueRecord)
        if status is not None:
            stmt = stmt.where(ReviewQueueRecord.status == status)
        if entry_type is not None:
            stmt = stmt.where(ReviewQueueRecord.entry_type == entry_type)
        rows = session.execute(stmt).scalars().all()
        return [ReviewQueueEntry.model_validate(row, from_attributes=True) for row in rows]


class BatchScope:
    """What one batch produced: the extraction results of its pages and the queue entries of its refused pages."""

    def __init__(self, result_ids: set[int], queue_ids: set[int]):
        self.result_ids = result_ids
        self.queue_ids = queue_ids

    def holds(self, entry_type: str, entry_id: int, payload) -> bool:
        """`payload` is the stored dict (a row) or the typed model (a listed entry)."""
        if entry_type == "new_template_needed":
            return entry_id in self.queue_ids
        result_id = payload.get("extraction_result_id") if isinstance(payload, dict) else getattr(payload, "extraction_result_id", None)
        return result_id in self.result_ids


def batch_scope(session, batch_id: str) -> BatchScope:
    """Raises ReviewEntryNotFoundError for a batch that does not exist (an empty batch is a real, empty scope)."""
    if session.get(BatchRecord, batch_id) is None:
        raise ReviewEntryNotFoundError(f"No batch found for id {batch_id}")
    result_ids: set[int] = set()
    queue_ids: set[int] = set()
    for result in session.execute(select(BatchDocumentRecord.result).where(BatchDocumentRecord.batch_id == batch_id)).scalars():
        for page in (result or {}).get("pages", []):
            if page.get("extraction_result_id"):
                result_ids.add(page["extraction_result_id"])
            if page.get("review_queue_id"):
                queue_ids.add(page["review_queue_id"])
    return BatchScope(result_ids, queue_ids)


def reader_by_result(session) -> dict[int, str]:
    """extraction_result_id -> who read it, from the batches (entries queued before the payload carried `reader`)."""
    readers: dict[int, str] = {}
    for result in session.execute(select(BatchDocumentRecord.result).where(BatchDocumentRecord.result.is_not(None))).scalars():
        for page in result.get("pages", []):
            if page.get("extraction_result_id") and page.get("reader"):
                readers[page["extraction_result_id"]] = page["reader"]
    return readers


def list_pending(entry_type: Optional[str] = None) -> list[ReviewQueueEntry]:
    return _list_entries(status="pending", entry_type=entry_type)


@router.post("/review/field-review", status_code=201)
def post_field_review(payload: FieldReviewPayload) -> dict:
    queue_id = enqueue_field_review(
        extraction_result_id=payload.extraction_result_id,
        field_id=payload.field_id,
        original_value=payload.original_value,
        reasons=payload.reasons,
    )
    return {"id": queue_id}


@router.post("/review/new-template-needed", status_code=201)
def post_new_template_needed(payload: NewTemplateNeededPayload) -> dict:
    queue_id = enqueue_new_template_needed(
        attempted_type_hint=payload.attempted_type_hint, rejection_reason=payload.rejection_reason
    )
    return {"id": queue_id}


@router.get("/review", response_model=list[ReviewQueueEntry])
def list_review_queue(status: Optional[str] = "pending", entry_type: Optional[str] = None, batch_id: Optional[str] = None) -> list[ReviewQueueEntry]:
    entries = _list_entries(status=status, entry_type=entry_type)
    if batch_id is None:
        return entries
    with SessionLocal() as session:
        try:
            scope = batch_scope(session, batch_id)
        except ReviewEntryNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
    return [e for e in entries if scope.holds(e.entry_type, e.id, e.payload)]


@router.patch("/review/{queue_id}")
def patch_review(queue_id: int, body: ReviewResolutionRequest) -> dict:
    with SessionLocal() as session:
        row = session.get(ReviewQueueRecord, queue_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"No review queue entry with id {queue_id}")

    try:
        if row.entry_type in VALUE_ENTRY_TYPES:
            if body.confirmed_value is None:
                raise HTTPException(
                    status_code=422, detail=f"confirmed_value is required to resolve a {row.entry_type} entry"
                )
            resolve_field_review(queue_id, body.confirmed_value)
        elif row.entry_type == "new_template_needed":
            if body.created_template_id is None:
                raise HTTPException(
                    status_code=422, detail="created_template_id is required to resolve a new_template_needed entry"
                )
            resolve_new_template_needed(queue_id, body.created_template_id)
        else:
            raise HTTPException(status_code=500, detail=f"unrecognized entry_type {row.entry_type!r}")
    except ReviewEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (ReviewTypeMismatchError, TemplateNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    return {"id": queue_id, "status": "resolved"}
