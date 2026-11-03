"""
Correcting a whole group, accepting what two readers agree on, scoping the queue to a batch, and the two clear buttons
(app/modules/review_groups.py, app/modules/housekeeping.py). These write straight to the review queue and results.
"""

import random
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

import app.config as config
from app.db import SessionLocal
from app.models.batch import BatchDocumentRecord, BatchRecord
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord
from app.modules import review_groups, review_queue
from app.modules.review_groups import BulkRefusedError, bulk_correct


def _result(template_id, fields):
    with SessionLocal() as session:
        row = ExtractionResultRecord(
            template_id=template_id, template_version=1, extracted_at=datetime.now(timezone.utc), overall_status="needs_review",
            fields={k: {"value": v, "confidence": 80.0, "needs_review": True, "approval_tier": "spot_check", "reasons": []} for k, v in fields.items()},
        )
        session.add(row)
        session.commit()
        return row.id


def _spot(result_id, field_id, value, why="no_validator", confidence=80.0, reader=None):
    return review_queue.enqueue_spot_check(result_id, field_id, value, confidence, why, reader=reader)


def _row(queue_id):
    with SessionLocal() as session:
        row = session.get(ReviewQueueRecord, queue_id)
        return row.status, row.resolution, row.payload


def _value(result_id, field_id):
    with SessionLocal() as session:
        return session.get(ExtractionResultRecord, result_id).fields[field_id]["value"]


def _batch(pages_by_doc, status="completed"):
    """A batch whose documents' stored results list these pages: [{extraction_result_id|review_queue_id, reader}, ...]."""
    batch_id = uuid.uuid4().hex
    with SessionLocal() as session:
        session.add(BatchRecord(id=batch_id, created_at=datetime.now(timezone.utc), type_hint=None))
        session.flush()  # the models declare no relationship(): the batch row must exist first
        for pos, pages in enumerate(pages_by_doc):
            session.add(
                BatchDocumentRecord(
                    batch_id=batch_id, position=pos, filename=f"d{pos}.pdf", status=status, attempts=1,
                    result={"pages": pages}, created_at=datetime.now(timezone.utc),
                )
            )
        session.commit()
    return batch_id


@pytest.fixture()
def template_id():
    return f"act_{uuid.uuid4().hex}"


@pytest.fixture()
def no_other_batches():
    def wipe():
        with SessionLocal() as session:
            session.execute(delete(BatchDocumentRecord))
            session.execute(delete(BatchRecord))
            session.commit()

    wipe()
    yield
    wipe()


# ------------------------------------------------------------------------------------------------ correct a group


def test_bulk_correct_writes_one_value_to_the_group_and_holds_a_sample_back(template_id):
    rids = [_result(template_id, {"hs": "HS CODE: 6109"}) for _ in range(20)]
    ids = [_spot(r, "hs", "HS CODE: 6109") for r in rids]
    out = bulk_correct(ids, "6109", audit_fraction=0.10, rng=random.Random(3))

    assert len(out["held_for_audit"]) == 2 and len(out["corrected"]) == 18
    for qid, rid in zip(ids, rids):
        status, resolution, payload = _row(qid)
        if qid in out["held_for_audit"]:
            assert status == "pending" and payload["audit"] is True and _value(rid, "hs") == "HS CODE: 6109"
        else:
            assert status == "resolved" and resolution["confirmed_value"] == "6109" and resolution["via"] == "bulk"
            assert _value(rid, "hs") == "6109"


def test_bulk_correct_also_takes_values_module_7_rejected(template_id):
    rids = [_result(template_id, {"hs": "HS CODE: 6109"}) for _ in range(2)]
    ids = [review_queue.enqueue_field_review(r, "hs", "HS CODE: 6109", ["format_invalid"], 85.0) for r in rids]
    out = bulk_correct(ids, "6109", rng=random.Random(1))
    assert len(out["corrected"]) == 1 and len(out["held_for_audit"]) == 1
    # a held field_review stays pending and cannot be swept into a second bulk correction
    held = out["held_for_audit"][0]
    with pytest.raises(BulkRefusedError) as exc:
        bulk_correct([held, out["corrected"][0]], "6109")
    assert exc.value.refused[held] == "audit_sample" and exc.value.refused[out["corrected"][0]] == "not_pending"


def test_bulk_correct_refuses_mixed_fields_an_empty_value_and_missing_entries(template_id):
    a = _spot(_result(template_id, {"a": "x"}), "a", "x")
    b = _spot(_result(template_id, {"b": "x"}), "b", "x")
    with pytest.raises(BulkRefusedError) as exc:
        bulk_correct([a, b], "y")
    assert set(exc.value.refused.values()) == {"not_one_field_of_one_template"}
    with pytest.raises(BulkRefusedError) as exc:
        bulk_correct([a, b], "   ")
    assert set(exc.value.refused.values()) == {"empty_value"}
    with pytest.raises(BulkRefusedError) as exc:
        bulk_correct([a, 10**9], "y")
    assert exc.value.refused == {10**9: "not_found"}
    assert _row(a)[0] == "pending" and _row(b)[0] == "pending"  # all or nothing


def test_bulk_correct_holds_the_value_to_the_fields_own_format_rule(template_id, monkeypatch):
    monkeypatch.setattr(review_groups, "get_template_version", lambda t, v: SimpleNamespace(fields=[SimpleNamespace(id="cur", validation_regex=r"[A-Z]{3}")]))
    ids = [_spot(_result(template_id, {"cur": "US D"}), "cur", "US D") for _ in range(2)]
    with pytest.raises(BulkRefusedError) as exc:
        bulk_correct(ids, "usd")
    assert set(exc.value.refused.values()) == {"format_invalid"}
    assert all(_row(i)[0] == "pending" for i in ids)
    assert len(bulk_correct(ids, "USD", rng=random.Random(2))["corrected"]) == 1


def test_bulk_correct_endpoint(client, template_id):
    ids = [_spot(_result(template_id, {"a": "x"}), "a", "x") for _ in range(3)]
    assert client.post("/review/bulk-correct", json={"entry_ids": ids[:1], "corrected_value": "y"}).status_code == 422  # one is not a group
    assert client.post("/review/bulk-correct", json={"entry_ids": ids, "corrected_value": ""}).status_code == 422
    resp = client.post("/review/bulk-correct", json={"entry_ids": ids, "corrected_value": "y"})
    assert resp.status_code == 200 and resp.json()["value"] == "y" and len(resp.json()["corrected"]) == 2


def test_value_groups_can_include_what_module_7_rejected(client, template_id):
    spot = _spot(_result(template_id, {"hs": "HS CODE: 1"}), "hs", "HS CODE: 1")
    rej = review_queue.enqueue_field_review(_result(template_id, {"hs": "HS CODE: 1"}), "hs", "HS CODE: 1", ["format_invalid"], 85.0)
    assert client.get("/review/groups", params={"by": "value", "template_id": template_id}).json()["total"] == 0
    groups = client.get("/review/groups", params={"by": "value", "template_id": template_id, "include_rejected": "true"}).json()["groups"]
    assert len(groups) == 1 and groups[0]["entry_ids"] == [spot, rej] and groups[0]["accept_ok"] is False


# ------------------------------------------------------------------------------------------------ one batch


def test_the_queue_can_be_scoped_to_one_batch(client, template_id, no_other_batches):
    mine = _result(template_id, {"a": "1"})
    theirs = _result(template_id, {"a": "1"})
    in_batch = _spot(mine, "a", "1")
    outside = _spot(theirs, "a", "1")
    refused = review_queue.enqueue_new_template_needed(None, "no_match")
    batch_id = _batch([[{"extraction_result_id": mine, "reader": "vlm"}, {"confirmed": False, "review_queue_id": refused}]])

    ids = {e["id"] for e in client.get("/review", params={"batch_id": batch_id}).json()}
    assert ids == {in_batch, refused}
    assert outside in {e["id"] for e in client.get("/review").json()}
    assert client.get("/review", params={"batch_id": "nope"}).status_code == 404


# ------------------------------------------------------------------------------------------------ accept what is agreed


def test_only_gated_model_reads_count_as_agreed(client, template_id, no_other_batches):
    gated = config.VLM_GATED_CONFIDENCE
    model_gated = _spot(_result(template_id, {"a": "1"}), "a", "1", why="below_auto_threshold", confidence=gated, reader="vlm")
    model_ungated = _spot(_result(template_id, {"a": "2"}), "a", "2", confidence=config.VLM_UNGATED_CONFIDENCE, reader="vlm")
    tesseract = _spot(_result(template_id, {"a": "3"}), "a", "3", confidence=gated, reader="ocr")
    rejected = review_queue.enqueue_field_review(_result(template_id, {"a": "4"}), "a", "4", ["format_invalid"], gated)
    blank = _spot(_result(template_id, {"a": ""}), "a", "", why="blank_optional", confidence=gated, reader="vlm")
    # written before the payload recorded the reader: recovered from the batch the page came in
    legacy_result = _result(template_id, {"a": "5"})
    legacy = _spot(legacy_result, "a", "5", why="below_auto_threshold", confidence=gated)
    _batch([[{"extraction_result_id": legacy_result, "reader": "vlm"}]])

    with SessionLocal() as session:
        agreed = {r.id for r in review_groups.agreed_rows(session)}
    assert {model_gated, legacy} <= agreed
    assert not ({model_ungated, tesseract, rejected, blank} & agreed)
    assert client.get("/review/agreed").json()["count"] >= 2


def test_accept_agreed_runs_a_bulk_accept_and_keeps_an_audit_sample(client, template_id, no_other_batches):
    gated = config.VLM_GATED_CONFIDENCE
    rids = [_result(template_id, {"a": "1"}) for _ in range(20)]
    ids = [_spot(r, "a", "1", why="below_auto_threshold", confidence=gated, reader="vlm") for r in rids]
    other = _spot(_result(template_id, {"a": "9"}), "a", "9", why="below_auto_threshold", confidence=gated, reader="vlm")
    batch_id = _batch([[{"extraction_result_id": r, "reader": "vlm"}] for r in rids])

    assert client.get("/review/agreed", params={"batch_id": batch_id}).json() == {"count": 20, "by_field": {"a": 20}}
    out = client.post("/review/accept-agreed", json={"batch_id": batch_id}).json()
    assert len(out["held_for_audit"]) == 2 and len(out["accepted"]) == 18
    assert _row(other)[0] == "pending"  # outside the batch: untouched
    for qid in out["accepted"]:
        status, resolution, _ = _row(qid)
        assert status == "resolved" and resolution["via"] == "bulk"  # a bulk accept is never counted as evidence
    assert client.post("/review/accept-agreed", json={"batch_id": "nope"}).status_code == 404


# ------------------------------------------------------------------------------------------------ clear buttons


def test_clearing_the_queue_needs_confirmation_and_leaves_resolved_entries(client, template_id, no_other_batches):
    pending = _spot(_result(template_id, {"a": "1"}), "a", "1")
    done = _spot(_result(template_id, {"a": "2"}), "a", "2")
    review_queue.resolve_field_review(done, "2")

    assert client.post("/review/clear", json={}).status_code == 422
    assert _row(pending)[0] == "pending"
    body = client.post("/review/clear", json={"confirm": True}).json()
    assert body["deleted"] >= 1
    with SessionLocal() as session:
        assert session.get(ReviewQueueRecord, pending) is None
        assert session.get(ReviewQueueRecord, done) is not None


def test_clearing_one_batchs_queue_leaves_the_rest(client, template_id, no_other_batches):
    mine = _spot(_result(template_id, {"a": "1"}), "a", "1")
    other = _spot(_result(template_id, {"a": "1"}), "a", "1")
    with SessionLocal() as session:
        mine_result = session.get(ReviewQueueRecord, mine).payload["extraction_result_id"]
    batch_id = _batch([[{"extraction_result_id": mine_result}]])
    assert client.post("/review/clear", json={"confirm": True, "batch_id": batch_id}).json()["deleted"] == 1
    with SessionLocal() as session:
        assert session.get(ReviewQueueRecord, mine) is None
        assert session.get(ReviewQueueRecord, other) is not None


def test_clearing_history_keeps_what_the_evidence_is_built_from(client, template_id, no_other_batches):
    checked_result = _result(template_id, {"a": "1"})
    checked = _spot(checked_result, "a", "1", why="below_auto_threshold")
    review_queue.resolve_field_review(checked, "1")
    unchecked_result = _result(template_id, {"a": "2"})
    pending = _spot(unchecked_result, "a", "2")
    _batch([[{"extraction_result_id": unchecked_result}]])

    assert client.post("/documents/history/clear", json={}).status_code == 422
    body = client.post("/documents/history/clear", json={"confirm": True}).json()
    assert body["batches"] >= 1 and body["results_kept_for_evidence"] >= 1
    with SessionLocal() as session:
        assert session.get(ExtractionResultRecord, unchecked_result) is None
        assert session.get(ReviewQueueRecord, pending) is None
        assert session.get(ExtractionResultRecord, checked_result) is not None
        assert session.get(ReviewQueueRecord, checked) is not None
    ev = client.get("/review/evidence").json()
    assert any(f["template_id"] == template_id and f["checked"] == 1 for f in ev["fields"])


def test_history_is_not_cleared_while_a_document_is_still_processing(client, template_id, no_other_batches):
    result = _result(template_id, {"a": "1"})
    _batch([[{"extraction_result_id": result}]], status="processing")
    resp = client.post("/documents/history/clear", json={"confirm": True})
    assert resp.status_code == 409
    with SessionLocal() as session:
        assert session.get(ExtractionResultRecord, result) is not None
