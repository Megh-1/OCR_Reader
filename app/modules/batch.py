"""
Batch processing (Phase 4): POST /documents/batch enqueues each uploaded document
as an RQ job on Redis and returns at once; GET /documents/batch/{id}/status reports
where every document stands and hands back the results of the finished ones.

The synchronous POST /documents/process is untouched. A batch job runs the same
pipeline (pipeline.prepare_document + pipeline.process_page) the endpoint does, so
a document gives the same answer either way.

Durable state lives in Postgres (batches / batch_documents), not in Redis: RQ keeps
job state only briefly, and a batch's status has to outlive that.

Failure handling, because the two kinds are not alike:
  * A file that cannot be ingested (wrong type, too big, corrupt) fails for good on
    the first attempt. The same bytes will fail the same way, so it is not retried.
  * Anything else that raises (a database or Tesseract hiccup, a timeout) is retried
    by RQ up to BATCH_MAX_RETRIES times with BATCH_RETRY_INTERVALS between attempts.
  * A retry must not redo pages that already finished: each finished page has
    written an extraction result and review-queue entries, and running it again
    would duplicate them. Progress is saved after every page and a retry resumes at
    the first unfinished one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from redis.exceptions import RedisError
from rq import Retry, get_current_job
from sqlalchemy import select

from app.config import (
    BATCH_JOB_TIMEOUT,
    BATCH_MAX_DOCUMENTS,
    BATCH_MAX_RETRIES,
    BATCH_RETRY_INTERVALS,
    MAX_UPLOAD_BYTES,
)
from app.db import SessionLocal
from app.job_queue import get_queue
from app.models.batch import BatchDocumentRecord, BatchRecord
from app.modules.ingestion import IngestionError
from app.modules.pipeline import ProcessDocumentResponse, prepare_document, process_page
from app.schemas.batch import (
    BatchDocumentStatus,
    BatchDocumentSubmitted,
    BatchStatusResponse,
    BatchSubmitResponse,
    BatchSummary,
    BatchTemplateSummary,
)

router = APIRouter()
log = logging.getLogger(__name__)

PENDING = ("queued", "processing", "retrying")
TERMINAL = ("completed", "failed")
ALL_STATUSES = PENDING + TERMINAL


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _batch_status(counts: dict[str, int]) -> str:
    """queued (nothing started) / processing (something still pending) / then how it ended."""
    total = sum(counts.values())
    if counts["queued"] == total:
        return "queued"
    if any(counts[s] for s in PENDING):
        return "processing"
    if counts["failed"] == total:
        return "failed"
    return "completed_with_errors" if counts["failed"] else "completed"


# --------------------------------------------------------------------------- submit


@router.post("/documents/batch", response_model=BatchSubmitResponse, status_code=202)
def submit_batch(files: list[UploadFile] = File(...), type_hint: Optional[str] = Form(None)) -> BatchSubmitResponse:
    """
    Accepts several documents, records them, enqueues one job each and returns a
    batch_id without processing anything. Nothing is enqueued unless the whole
    request is acceptable: too many files, or one over the size limit, rejects it all.
    (Every OTHER problem with a file - wrong type, corrupt - is that file's own failure,
    reported per document once a worker reaches it.)

    A sync def, so FastAPI runs it in a worker thread: reading up to
    BATCH_MAX_DOCUMENTS uploads and writing them to Postgres must not stall the event loop.
    """
    if len(files) > BATCH_MAX_DOCUMENTS:
        raise HTTPException(
            status_code=422,
            detail={"reason_code": "too_many_documents",
                    "message": f"{len(files)} documents submitted; a batch holds at most {BATCH_MAX_DOCUMENTS}"},
        )

    uploads = []
    for upload in files:
        data = upload.file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=422,
                detail={"reason_code": "oversized",
                        "message": f"{upload.filename!r} exceeds the max size of {MAX_UPLOAD_BYTES} bytes"},
            )
        uploads.append((upload.filename or "unnamed", data))

    queue = get_queue()
    try:
        queue.connection.ping()
    except RedisError as exc:
        # Before writing anything: a batch that could never run must not leave rows saying "queued".
        raise HTTPException(
            status_code=503,
            detail={"reason_code": "queue_unavailable", "message": f"the job queue is unreachable ({type(exc).__name__})"},
        )

    batch_id = uuid.uuid4().hex
    now = _now()
    with SessionLocal() as session:
        session.add(BatchRecord(id=batch_id, created_at=now, type_hint=type_hint))
        # The models declare no relationship(), so SQLAlchemy does not know the documents depend on
        # the batch row and may insert them first (it did, and hit the foreign key).
        session.flush()
        records = [
            BatchDocumentRecord(batch_id=batch_id, position=i, filename=name, status="queued", attempts=0,
                                payload=data, created_at=now)
            for i, (name, data) in enumerate(uploads)
        ]
        session.add_all(records)
        session.commit()
        document_ids = [r.id for r in records]

    retry = Retry(max=BATCH_MAX_RETRIES, interval=list(BATCH_RETRY_INTERVALS))
    enqueued = 0
    try:
        for document_id in document_ids:
            queue.enqueue(
                process_batch_document, document_id,
                job_id=f"batchdoc-{document_id}", retry=retry, job_timeout=BATCH_JOB_TIMEOUT,
                result_ttl=3600, failure_ttl=86400,
            )
            enqueued += 1
    except RedisError as exc:
        # Redis went away mid-way. Documents already enqueued will still run; the rest never will,
        # so say so on their rows rather than leave them "queued" forever.
        _fail_unqueued(document_ids[enqueued:], f"could not be queued: {type(exc).__name__}")
        raise HTTPException(
            status_code=503,
            detail={"reason_code": "queue_unavailable", "batch_id": batch_id,
                    "message": f"the queue failed after {enqueued} of {len(document_ids)} documents were queued; "
                               f"see the batch status for which"},
        )

    return BatchSubmitResponse(
        batch_id=batch_id,
        status="queued",
        document_count=len(uploads),
        documents=[BatchDocumentSubmitted(index=i, filename=name, status="queued") for i, (name, _) in enumerate(uploads)],
        status_url=f"/documents/batch/{batch_id}/status",
    )


def _fail_unqueued(document_ids: list[int], error: str) -> None:
    with SessionLocal() as session:
        for document_id in document_ids:
            record = session.get(BatchDocumentRecord, document_id)
            record.status, record.error, record.payload, record.finished_at = "failed", error, None, _now()
        session.commit()


# --------------------------------------------------------------------------- status


@router.get("/documents/batch", response_model=list[BatchSummary])
def list_batches(limit: int = Query(20, ge=1, le=100)) -> list[BatchSummary]:
    """Recent batches, newest first, with per-status document counts (no results, no payloads)."""
    with SessionLocal() as session:
        batches = session.scalars(select(BatchRecord).order_by(BatchRecord.created_at.desc()).limit(limit)).all()
        ids = [b.id for b in batches]
        rows = session.execute(
            select(BatchDocumentRecord.batch_id, BatchDocumentRecord.status).where(BatchDocumentRecord.batch_id.in_(ids))
        ).all() if ids else []
    counts = {b.id: {status: 0 for status in ALL_STATUSES} for b in batches}
    for batch_id, status in rows:
        counts[batch_id][status] += 1
    return [
        BatchSummary(batch_id=b.id, created_at=b.created_at, type_hint=b.type_hint, status=_batch_status(counts[b.id]),
                     document_count=sum(counts[b.id].values()), counts=counts[b.id])
        for b in batches
    ]


@router.get("/documents/batch/{batch_id}/status", response_model=BatchStatusResponse)
def batch_status(batch_id: str) -> BatchStatusResponse:
    with SessionLocal() as session:
        batch = session.get(BatchRecord, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"No batch found for id {batch_id}")
        records = session.scalars(
            select(BatchDocumentRecord).where(BatchDocumentRecord.batch_id == batch_id).order_by(BatchDocumentRecord.position)
        ).all()

        counts = {status: 0 for status in ALL_STATUSES}
        documents = []
        summary = BatchTemplateSummary(by_template={}, unmatched={})
        for record in records:
            counts[record.status] += 1
            partial = record.result or {}
            for page in partial.get("pages", []):
                summary.pages += 1
                for engine in page.get("engines_down") or []:
                    summary.engines_down[engine] = summary.engines_down.get(engine, 0) + 1
                if page.get("confirmed"):
                    name = (page.get("result") or {}).get("template_id", "?")
                    summary.by_template[name] = summary.by_template.get(name, 0) + 1
                else:
                    reason = page.get("reason") or "unknown"
                    summary.unmatched[reason] = summary.unmatched.get(reason, 0) + 1
            documents.append(
                BatchDocumentStatus(
                    index=record.position,
                    filename=record.filename,
                    status=record.status,
                    attempts=record.attempts,
                    error=record.error,
                    pages_done=len(partial.get("pages", [])),
                    page_count=partial.get("page_count"),
                    result=record.result if record.status == "completed" else None,
                    started_at=record.started_at,
                    finished_at=record.finished_at,
                )
            )
        return BatchStatusResponse(
            batch_id=batch.id,
            status=_batch_status(counts),
            created_at=batch.created_at,
            type_hint=batch.type_hint,
            counts=counts,
            documents=documents,
            summary=summary,
        )


# --------------------------------------------------------------------------- the job


def process_batch_document(document_id: int) -> None:
    """
    The RQ job: runs one batch document through the pipeline. Must be importable by
    name from a worker process, so it lives at module level.
    """
    with SessionLocal() as session:
        record = session.get(BatchDocumentRecord, document_id)
        if record is None:
            log.warning("batch document %s no longer exists; dropping its job", document_id)
            return
        if record.status in TERMINAL:
            return  # a redelivered job must not redo finished work
        record.status = "processing"
        record.attempts += 1
        record.started_at = record.started_at or _now()
        payload, filename = record.payload, record.filename
        type_hint = session.get(BatchRecord, record.batch_id).type_hint
        attempt = record.attempts
        session.commit()

    log.info("batch document %s (%s): attempt %d", document_id, filename, attempt)
    try:
        _run(document_id, payload, filename, type_hint)
    except IngestionError as exc:
        # Permanent: return normally so RQ does not retry it.
        _finish(document_id, "failed", error=f"{exc.reason_code}: {exc}")
        log.info("batch document %s failed ingestion (%s); not retrying", document_id, exc.reason_code)
        return
    except Exception as exc:
        job = get_current_job()
        # RQ decides whether to retry from the same field; ask it, rather than counting attempts ourselves.
        will_retry = job is not None and job.should_retry
        _record_error(document_id, "retrying" if will_retry else "failed", f"{type(exc).__name__}: {exc}")
        log.exception("batch document %s attempt %d failed (%s)", document_id, attempt,
                      "will retry" if will_retry else "giving up")
        raise


def _run(document_id: int, payload: bytes, filename: str, type_hint: Optional[str]) -> None:
    pages, metadata, text_layers = prepare_document(payload, filename, type_hint)

    with SessionLocal() as session:
        done = list((session.get(BatchDocumentRecord, document_id).result or {}).get("pages", []))

    # Ingestion is deterministic, so page i of a retry is page i of the first attempt.
    for i in range(len(done), len(pages)):
        done.append(process_page(pages[i], i, type_hint, text_layers[i]).model_dump(mode="json"))
        _save(document_id, {"filename": metadata.filename, "page_count": metadata.page_count, "pages": done})

    # Validate the assembled body against the response model the synchronous endpoint returns.
    body = ProcessDocumentResponse(filename=metadata.filename, page_count=metadata.page_count, pages=done)
    _finish(document_id, "completed", result=body.model_dump(mode="json"))


def _save(document_id: int, result: dict) -> None:
    with SessionLocal() as session:
        session.get(BatchDocumentRecord, document_id).result = result
        session.commit()


def _finish(document_id: int, status: str, error: Optional[str] = None, result: Optional[dict] = None) -> None:
    with SessionLocal() as session:
        record = session.get(BatchDocumentRecord, document_id)
        record.status = status
        record.finished_at = _now()
        record.payload = None  # terminal: the upload is no longer needed
        if result is not None:
            record.result = result
            record.error = None
        if error is not None:
            record.error = error
        session.commit()


def _record_error(document_id: int, status: str, error: str) -> None:
    """Note a failed attempt. A document that is going to be retried keeps its payload; one that is not does not."""
    with SessionLocal() as session:
        record = session.get(BatchDocumentRecord, document_id)
        record.status, record.error = status, error
        if status == "failed":
            record.finished_at, record.payload = _now(), None
        session.commit()
