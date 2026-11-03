"""
Clearing the dev backlog: the pending review queue, and the history of processed documents.

Both are explicit, confirmed, permanent deletes that a person asks for; nothing here runs on its own. What they leave
alone is deliberate:

* a RESOLVED review entry is never deleted. It is the ledger audited auto-approval counts (trust.py, /review/evidence)
  - what a person checked, and what they changed - so wiping it would erase the only evidence there is.
* an extraction result that a resolved entry points at is kept too, because the evidence query joins through it to
  know which template version and field the check was about.
* history is refused while a document is still queued or processing: its worker would write into rows that are gone.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.db import SessionLocal
from app.models.batch import BatchDocumentRecord, BatchRecord
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord
from app.modules.review_queue import ReviewEntryNotFoundError, batch_scope
from app.modules.trust import clear_trust_cache

router = APIRouter()

IN_FLIGHT = ("queued", "processing", "retrying")


class ConfirmedRequest(BaseModel):
    confirm: bool = False
    batch_id: Optional[str] = None  # review/clear only: clear just what this batch left pending
    field_id: Optional[str] = None  # review/clear only: clear just this field's pending values ("leave this field blank")


def _require_confirmation(body: ConfirmedRequest) -> None:
    if not body.confirm:
        raise HTTPException(status_code=422, detail="This permanently deletes data; send {\"confirm\": true} to proceed.")


def clear_pending_review(batch_id: Optional[str] = None, field_id: Optional[str] = None) -> dict:
    """Delete every PENDING review entry (of one batch and/or one field, when given). Resolved entries stay. A cleared
    value is not settled: it stays in its result as the machine's read, and the strict JSON export leaves it blank."""
    with SessionLocal() as session:
        stmt = select(ReviewQueueRecord).where(ReviewQueueRecord.status == "pending")
        rows = list(session.execute(stmt).scalars())
        if batch_id is not None:
            scope = batch_scope(session, batch_id)
            rows = [r for r in rows if scope.holds(r.entry_type, r.id, r.payload)]
        if field_id is not None:
            rows = [r for r in rows if r.payload.get("field_id") == field_id]
        counts: dict[str, int] = {}
        for r in rows:
            counts[r.entry_type] = counts.get(r.entry_type, 0) + 1
        if rows:
            session.execute(delete(ReviewQueueRecord).where(ReviewQueueRecord.id.in_([r.id for r in rows])))
        session.commit()
    return {"deleted": sum(counts.values()), "by_type": counts}


def clear_history() -> dict:
    """Delete batches, their documents, extraction results and pending review entries; keep resolved review entries and
    the results they refer to. Raises HistoryBusyError while any document is still being processed."""
    with SessionLocal() as session:
        busy = session.scalar(select(func.count()).select_from(BatchDocumentRecord).where(BatchDocumentRecord.status.in_(IN_FLIGHT)))
        if busy:
            raise HistoryBusyError(busy)

        kept_ids = {
            int(i)
            for i in session.execute(
                select(ReviewQueueRecord.payload["extraction_result_id"].astext).where(
                    ReviewQueueRecord.status == "resolved", ReviewQueueRecord.entry_type.in_(("field_review", "spot_check"))
                )
            ).scalars()
            if i is not None
        }
        pending = session.execute(delete(ReviewQueueRecord).where(ReviewQueueRecord.status == "pending")).rowcount
        docs = session.execute(delete(BatchDocumentRecord)).rowcount
        batches = session.execute(delete(BatchRecord)).rowcount
        results = session.execute(delete(ExtractionResultRecord).where(ExtractionResultRecord.id.not_in(kept_ids))).rowcount if kept_ids else session.execute(delete(ExtractionResultRecord)).rowcount
        session.commit()
    clear_trust_cache()
    return {"batches": batches, "documents": docs, "results": results, "pending_review_entries": pending, "results_kept_for_evidence": len(kept_ids)}


class HistoryBusyError(Exception):
    def __init__(self, busy: int):
        super().__init__(f"{busy} document(s) are still being processed")
        self.busy = busy


@router.post("/review/clear")
def post_clear_review(body: ConfirmedRequest) -> dict:
    _require_confirmation(body)
    try:
        return clear_pending_review(body.batch_id, body.field_id)
    except ReviewEntryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/documents/history/clear")
def post_clear_history(body: ConfirmedRequest) -> dict:
    _require_confirmation(body)
    try:
        return clear_history()
    except HistoryBusyError as exc:
        raise HTTPException(status_code=409, detail=f"{exc} Wait for the running batch to finish, then try again.")
