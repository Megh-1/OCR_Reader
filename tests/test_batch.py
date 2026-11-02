"""
Batch processing: POST /documents/batch, the RQ worker, retries, and the status endpoint.

Runs against an in-memory Redis (fakeredis) and RQ's SimpleWorker, which executes a job
in the calling process - so it behaves the same on Windows and Linux, and the OCR mocks
that the other pipeline tests use apply to it. A real Redis server has not been used.
"""

import io
import re
from unittest.mock import Mock

import fakeredis
import fitz
import pytest
from redis import Redis
from rq import SimpleWorker
from sqlalchemy import func, select

import app.job_queue as job_queue
import app.modules.batch as batch
import app.modules.field_extraction as field_extraction
import app.modules.type_identification as type_identification
from app.config import BATCH_MAX_RETRIES
from app.db import SessionLocal
from app.models.batch import BatchDocumentRecord, BatchRecord
from app.models.extraction_result import ExtractionResultRecord
from app.modules.pipeline import PageResult, process_page as real_process_page
from tests.test_pipeline import make_image_bytes, save_template


@pytest.fixture()
def redis_conn(monkeypatch):
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    job_queue.set_connection(conn)
    monkeypatch.setattr(batch, "BATCH_RETRY_INTERVALS", (0,))  # retry at once; the delay itself is the scheduler's job
    yield conn
    job_queue.set_connection(None)


def run_worker(conn) -> None:
    """Drain the queue, the way `python -m app.worker --burst` does."""
    queue = job_queue.get_queue()
    SimpleWorker([queue], connection=conn).work(burst=True)


def ocr_reads_a_name(monkeypatch):
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(
        field_extraction.pytesseract, "image_to_data", Mock(return_value={"text": ["John", "Doe"], "conf": [95, 92]})
    )


@pytest.fixture()
def name_template(client, unique_doc_type):
    save_template(client, unique_doc_type, [
        {"id": "name", "label": "Name", "bbox": [0.1, 0.3, 0.5, 0.4], "data_type": "string",
         "validation_regex": r"^[A-Za-z ]+$", "confidence_floor": 0.5},
    ])
    return unique_doc_type


def submit(client, *files, type_hint=None):
    payload = [("files", (name, data, mime)) for name, data, mime in files]
    return client.post("/documents/batch", files=payload, data={"type_hint": type_hint} if type_hint else {})


def image(name="a.jpg"):
    return (name, make_image_bytes(), "image/jpeg")


def status(client, batch_id):
    resp = client.get(f"/documents/batch/{batch_id}/status")
    assert resp.status_code == 200
    return resp.json()


def rows(batch_id):
    with SessionLocal() as session:
        return session.scalars(
            select(BatchDocumentRecord).where(BatchDocumentRecord.batch_id == batch_id).order_by(BatchDocumentRecord.position)
        ).all()


def extraction_count(template_id):
    with SessionLocal() as session:
        return session.scalar(
            select(func.count()).select_from(ExtractionResultRecord).where(ExtractionResultRecord.template_id == template_id)
        )


def two_page_pdf() -> bytes:
    doc = fitz.open()
    for _ in range(2):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


# ------------------------------------------------------------------------- submit


def test_submit_returns_a_batch_id_at_once_and_processes_nothing(client, redis_conn, name_template):
    resp = submit(client, image("a.jpg"), image("b.jpg"), type_hint=name_template)

    assert resp.status_code == 202
    body = resp.json()
    assert re.fullmatch(r"[0-9a-f]{32}", body["batch_id"])
    assert body["document_count"] == 2 and body["status"] == "queued"
    assert [d["filename"] for d in body["documents"]] == ["a.jpg", "b.jpg"]
    assert body["status_url"] == f"/documents/batch/{body['batch_id']}/status"

    assert job_queue.get_queue().count == 2  # two jobs waiting; no worker has run
    got = status(client, body["batch_id"])
    assert got["status"] == "queued" and got["counts"]["queued"] == 2
    assert all(d["result"] is None for d in got["documents"])
    assert all(r.payload is not None for r in rows(body["batch_id"]))  # the upload is held for the worker
    assert extraction_count(name_template) == 0


def test_an_empty_submission_is_rejected(client, redis_conn):
    assert client.post("/documents/batch", data={}).status_code == 422


def test_too_many_documents_rejects_the_whole_batch(client, redis_conn, monkeypatch):
    monkeypatch.setattr(batch, "BATCH_MAX_DOCUMENTS", 2)

    resp = submit(client, image("1.jpg"), image("2.jpg"), image("3.jpg"))

    assert resp.status_code == 422 and resp.json()["detail"]["reason_code"] == "too_many_documents"
    assert job_queue.get_queue().count == 0


def test_an_oversized_file_rejects_the_whole_batch(client, redis_conn, monkeypatch):
    monkeypatch.setattr(batch, "MAX_UPLOAD_BYTES", 50)

    resp = submit(client, ("tiny.txt", b"x", "text/plain"), image("big.jpg"))

    assert resp.status_code == 422 and resp.json()["detail"]["reason_code"] == "oversized"
    assert "big.jpg" in resp.json()["detail"]["message"]
    assert job_queue.get_queue().count == 0  # the small file was not queued on its own


def test_an_unreachable_queue_is_a_503_and_leaves_nothing_behind(client):
    job_queue.set_connection(Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2))
    try:
        with SessionLocal() as session:
            before = session.scalar(select(func.count()).select_from(BatchRecord))

        resp = submit(client, image())

        assert resp.status_code == 503 and resp.json()["detail"]["reason_code"] == "queue_unavailable"
        with SessionLocal() as session:
            assert session.scalar(select(func.count()).select_from(BatchRecord)) == before
    finally:
        job_queue.set_connection(None)


# ------------------------------------------------------------------------- processing


def test_a_worker_completes_every_document_and_the_status_carries_the_results(client, redis_conn, name_template, monkeypatch):
    ocr_reads_a_name(monkeypatch)
    batch_id = submit(client, image("a.jpg"), image("b.jpg"), type_hint=name_template).json()["batch_id"]

    run_worker(redis_conn)

    got = status(client, batch_id)
    assert got["status"] == "completed" and got["counts"] == {"queued": 0, "processing": 0, "retrying": 0, "completed": 2, "failed": 0}
    for doc in got["documents"]:
        assert doc["status"] == "completed" and doc["attempts"] == 1 and doc["error"] is None
        assert doc["pages_done"] == doc["page_count"] == 1
        page = doc["result"]["pages"][0]
        assert page["confirmed"] is True and page["result"]["fields"]["name"]["value"] == "John Doe"
        assert page["result"]["fields"]["name"]["approval_tier"] == "auto"
    assert extraction_count(name_template) == 2
    assert all(r.payload is None for r in rows(batch_id))  # done with the uploads


def test_a_batch_result_matches_the_synchronous_endpoint(client, redis_conn, name_template, monkeypatch):
    ocr_reads_a_name(monkeypatch)
    sync = client.post("/documents/process", files={"file": ("a.jpg", make_image_bytes(), "image/jpeg")},
                       data={"type_hint": name_template}).json()
    batch_id = submit(client, image("a.jpg"), type_hint=name_template).json()["batch_id"]
    run_worker(redis_conn)

    via_batch = status(client, batch_id)["documents"][0]["result"]

    def comparable(body):  # everything but the ids the database assigned
        page = body["pages"][0]
        return body["filename"], body["page_count"], page["confirmed"], page["result"]["fields"], page["result"]["overall_status"]

    assert comparable(via_batch) == comparable(sync)


def test_a_document_that_cannot_be_ingested_fails_once_and_holds_up_nothing(client, redis_conn, name_template, monkeypatch):
    ocr_reads_a_name(monkeypatch)
    batch_id = submit(client, ("notes.txt", b"just some text", "text/plain"), image("ok.jpg"), type_hint=name_template).json()["batch_id"]

    run_worker(redis_conn)

    got = status(client, batch_id)
    bad, good = got["documents"]
    assert bad["status"] == "failed" and bad["attempts"] == 1  # the same bytes would fail the same way
    assert re.match(r"\w+: ", bad["error"]) and bad["result"] is None
    assert good["status"] == "completed"
    assert got["status"] == "completed_with_errors"
    assert job_queue.get_queue().failed_job_registry.count == 0  # it was not treated as a crash to retry


def test_a_transient_failure_is_retried_and_then_succeeds(client, redis_conn, name_template, monkeypatch):
    ocr_reads_a_name(monkeypatch)
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database went away")
        return real_process_page(*args, **kwargs)

    monkeypatch.setattr(batch, "process_page", flaky)
    batch_id = submit(client, image(), type_hint=name_template).json()["batch_id"]

    run_worker(redis_conn)

    doc = status(client, batch_id)["documents"][0]
    assert doc["status"] == "completed" and doc["attempts"] == 2 and doc["error"] is None
    assert extraction_count(name_template) == 1  # the failed attempt wrote nothing, the retry wrote one


def test_retries_are_bounded_and_the_document_ends_up_failed_with_its_last_error(client, redis_conn, name_template, monkeypatch):
    monkeypatch.setattr(batch, "process_page", Mock(side_effect=RuntimeError("boom")))
    batch_id = submit(client, image(), type_hint=name_template).json()["batch_id"]

    run_worker(redis_conn)

    doc = status(client, batch_id)["documents"][0]
    assert doc["status"] == "failed"
    assert doc["attempts"] == 1 + BATCH_MAX_RETRIES  # the first try, then every retry RQ was given
    assert doc["error"] == "RuntimeError: boom"
    assert job_queue.get_queue().failed_job_registry.count == 1
    assert rows(batch_id)[0].payload is None


def test_a_retry_resumes_at_the_first_unfinished_page(client, redis_conn, monkeypatch):
    """Finished pages have already written results and review entries; redoing them would duplicate both."""
    seen = []

    def fake_process_page(page_bytes, page_index, type_hint, text_layer=None):
        seen.append(page_index)
        if page_index == 1 and seen.count(1) == 1:
            raise RuntimeError("page 2 hit a snag")
        return PageResult(page_index=page_index, confirmed=False, reason="no_match")

    monkeypatch.setattr(batch, "process_page", fake_process_page)
    batch_id = submit(client, ("two.pdf", two_page_pdf(), "application/pdf")).json()["batch_id"]

    run_worker(redis_conn)

    assert seen == [0, 1, 1]  # page 0 once; page 1 twice; nothing repeated
    doc = status(client, batch_id)["documents"][0]
    assert doc["status"] == "completed" and doc["attempts"] == 2
    assert doc["pages_done"] == doc["page_count"] == 2
    assert [p["page_index"] for p in doc["result"]["pages"]] == [0, 1]


def test_a_redelivered_job_for_a_finished_document_does_nothing(client, redis_conn, name_template, monkeypatch):
    ocr_reads_a_name(monkeypatch)
    batch_id = submit(client, image(), type_hint=name_template).json()["batch_id"]
    run_worker(redis_conn)
    document_id = rows(batch_id)[0].id

    batch.process_batch_document(document_id)

    assert rows(batch_id)[0].attempts == 1 and extraction_count(name_template) == 1


# ------------------------------------------------------------------------- status


def test_an_unknown_batch_is_a_404(client, redis_conn):
    assert client.get("/documents/batch/deadbeef/status").status_code == 404


@pytest.mark.parametrize(
    "counts, expected",
    [
        ({"queued": 3, "processing": 0, "retrying": 0, "completed": 0, "failed": 0}, "queued"),
        ({"queued": 1, "processing": 1, "retrying": 0, "completed": 1, "failed": 0}, "processing"),
        ({"queued": 0, "processing": 0, "retrying": 1, "completed": 2, "failed": 0}, "processing"),
        ({"queued": 0, "processing": 0, "retrying": 0, "completed": 3, "failed": 0}, "completed"),
        ({"queued": 0, "processing": 0, "retrying": 0, "completed": 2, "failed": 1}, "completed_with_errors"),
        ({"queued": 0, "processing": 0, "retrying": 0, "completed": 0, "failed": 3}, "failed"),
    ],
)
def test_the_batch_status_is_derived_from_its_documents(counts, expected):
    assert batch._batch_status(counts) == expected


# ------------------------------------------------------------------------- what a failed attempt leaves behind


def _queued_document(client, redis_conn, name_template):
    batch_id = submit(client, image(), type_hint=name_template).json()["batch_id"]
    return rows(batch_id)[0].id


def _fail_one_attempt(monkeypatch, document_id, *, retries_left):
    """Run the job function once, with process_page failing and RQ saying whether a retry remains."""
    monkeypatch.setattr(batch, "process_page", Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(batch, "get_current_job", lambda: Mock(should_retry=retries_left))
    with pytest.raises(RuntimeError):
        batch.process_batch_document(document_id)
    with SessionLocal() as session:
        return session.get(BatchDocumentRecord, document_id)


def test_a_failed_attempt_with_retries_left_is_retrying_and_keeps_its_upload(client, redis_conn, name_template, monkeypatch):
    record = _fail_one_attempt(monkeypatch, _queued_document(client, redis_conn, name_template), retries_left=True)

    assert record.status == "retrying" and record.attempts == 1
    assert record.error == "RuntimeError: boom"
    assert record.payload is not None and record.finished_at is None  # the retry will need the bytes


def test_a_failed_attempt_with_no_retries_left_is_failed_and_drops_its_upload(client, redis_conn, name_template, monkeypatch):
    record = _fail_one_attempt(monkeypatch, _queued_document(client, redis_conn, name_template), retries_left=False)

    assert record.status == "failed" and record.error == "RuntimeError: boom"
    assert record.payload is None and record.finished_at is not None


def test_a_half_finished_document_reports_its_progress_but_not_a_result(client, redis_conn, monkeypatch):
    def page_two_fails(page_bytes, page_index, type_hint, text_layer=None):
        if page_index == 1:
            raise RuntimeError("page 2 hit a snag")
        return PageResult(page_index=page_index, confirmed=False, reason="no_match")

    monkeypatch.setattr(batch, "process_page", page_two_fails)
    monkeypatch.setattr(batch, "get_current_job", lambda: Mock(should_retry=True))
    batch_id = submit(client, ("two.pdf", two_page_pdf(), "application/pdf")).json()["batch_id"]
    with pytest.raises(RuntimeError):
        batch.process_batch_document(rows(batch_id)[0].id)

    doc = status(client, batch_id)["documents"][0]

    assert doc["status"] == "retrying" and doc["error"] == "RuntimeError: page 2 hit a snag"
    assert doc["pages_done"] == 1 and doc["page_count"] == 2  # how far it got, so a caller can show progress
    assert doc["result"] is None  # but a partial body is not a result


def test_status_summarises_where_the_pages_went(client):
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.models.batch import BatchDocumentRecord, BatchRecord

    import uuid

    batch_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    pages = [
        {"page_index": 0, "confirmed": True, "result": {"template_id": "whole_form"}},
        {"page_index": 1, "confirmed": True, "result": {"template_id": "part_form"}},
        {"page_index": 2, "confirmed": False, "reason": "no_match"},
    ]
    with SessionLocal() as session:
        session.add(BatchRecord(id=batch_id, created_at=now, type_hint=None))
        session.flush()
        session.add(BatchDocumentRecord(batch_id=batch_id, position=0, filename="a.pdf", status="completed", attempts=1,
                                        result={"page_count": 3, "pages": pages}, created_at=now))
        session.commit()
    body = client.get(f"/documents/batch/{batch_id}/status").json()
    assert body["summary"] == {"pages": 3, "by_template": {"whole_form": 1, "part_form": 1}, "unmatched": {"no_match": 1}}
