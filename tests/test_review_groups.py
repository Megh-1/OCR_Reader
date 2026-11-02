"""
Review grouping, bulk accept and the evidence ledger (app/modules/review_groups.py, app/utils/stats.py).

These write straight to the review queue and extraction results, so no OCR runs.
"""

import random
import uuid
from datetime import datetime, timezone

import pytest

from app.db import SessionLocal
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord
from app.modules import review_queue
from app.modules.review_groups import BulkRefusedError, bulk_accept
from app.utils.stats import clean_checks_needed, clopper_pearson_upper


def _result(template_id, fields):
    with SessionLocal() as session:
        row = ExtractionResultRecord(
            template_id=template_id, template_version=1, extracted_at=datetime.now(timezone.utc), overall_status="complete",
            fields={k: {"value": v, "confidence": 80.0, "needs_review": False, "approval_tier": "spot_check", "reasons": []} for k, v in fields.items()},
        )
        session.add(row)
        session.commit()
        return row.id


def _spot(result_id, field_id, value, why="no_validator", confidence=80.0):
    return review_queue.enqueue_spot_check(result_id, field_id, value, confidence, why)


def _status(queue_id):
    with SessionLocal() as session:
        row = session.get(ReviewQueueRecord, queue_id)
        return row.status, row.resolution, row.payload


@pytest.fixture()
def template_id():
    return f"grp_{uuid.uuid4().hex}"


# ------------------------------------------------------------------------------------------------ statistics


def test_rule_of_three_and_the_ten_thousand_target():
    assert clopper_pearson_upper(0, 100) == pytest.approx(1 - 0.05 ** (1 / 100), abs=1e-9)
    assert clopper_pearson_upper(0, 0) == 1.0  # nothing checked says nothing
    assert clean_checks_needed(0.0001) == 29956  # 1 error in 10,000 at 95% needs ~30,000 clean checks
    assert clopper_pearson_upper(0, 29956) <= 0.0001 < clopper_pearson_upper(0, 29955)


def test_upper_bound_grows_with_errors_and_matches_known_value():
    # 1 error in 20: the textbook two-sided 95% Clopper-Pearson interval ends at 0.2487, which is the one-sided
    # bound at 97.5%; at the one-sided 95% this module uses it is 0.2161
    assert clopper_pearson_upper(1, 20, confidence=0.975) == pytest.approx(0.2487, abs=5e-4)
    assert clopper_pearson_upper(1, 20) == pytest.approx(0.2161, abs=5e-4)
    assert clopper_pearson_upper(0, 200) < clopper_pearson_upper(1, 200) < clopper_pearson_upper(3, 200)


# ------------------------------------------------------------------------------------------------ bulk accept


def test_bulk_accept_confirms_values_but_holds_a_random_sample_back(template_id):
    ids = [_spot(_result(template_id, {"cur": "USD"}), "cur", "USD") for _ in range(20)]
    out = bulk_accept(ids, audit_fraction=0.10, rng=random.Random(7))

    assert len(out["held_for_audit"]) == 2 and len(out["accepted"]) == 18
    for i in out["accepted"]:
        status, resolution, _ = _status(i)
        assert status == "resolved" and resolution["via"] == "bulk" and resolution["confirmed_value"] == "USD"
    for i in out["held_for_audit"]:
        status, resolution, payload = _status(i)
        assert status == "pending" and resolution is None and payload["audit"] is True


def test_bulk_accept_always_leaves_at_least_one_for_a_person(template_id):
    ids = [_spot(_result(template_id, {"cur": "USD"}), "cur", "USD") for _ in range(2)]
    out = bulk_accept(ids, audit_fraction=0.05)
    assert len(out["held_for_audit"]) == 1 and len(out["accepted"]) == 1


def test_an_audit_sample_cannot_be_swept_into_a_later_bulk_accept(template_id):
    ids = [_spot(_result(template_id, {"cur": "USD"}), "cur", "USD") for _ in range(4)]
    held = bulk_accept(ids, audit_fraction=0.25, rng=random.Random(1))["held_for_audit"]
    with pytest.raises(BulkRefusedError) as err:
        bulk_accept(held)
    assert err.value.refused == {held[0]: "audit_sample"}


def test_a_single_entry_is_just_accepted(template_id):
    [i] = [_spot(_result(template_id, {"cur": "USD"}), "cur", "USD")]
    assert bulk_accept([i])["held_for_audit"] == []


def test_bulk_accept_refuses_everything_rejected_by_module_7_and_empty_or_missing(template_id):
    good = _spot(_result(template_id, {"a": "x"}), "a", "x")
    rejected = review_queue.enqueue_field_review(_result(template_id, {"a": "x"}), "a", "x", ["format_invalid"], 91.0)
    blank = _spot(_result(template_id, {"a": ""}), "a", "", why="blank_optional")
    empty_but_not_optional = _spot(_result(template_id, {"a": " "}), "a", " ")

    with pytest.raises(BulkRefusedError) as err:
        bulk_accept([good, rejected, blank, empty_but_not_optional, 999_999_999])
    assert err.value.refused == {
        rejected: "not_a_spot_check",
        blank: "needs_a_look:blank_optional",
        empty_but_not_optional: "empty_value",
        999_999_999: "not_found",
    }
    assert _status(good)[0] == "pending"  # all or nothing: the eligible one was not accepted either


def test_bulk_accept_refuses_an_entry_that_is_already_resolved(template_id):
    i = _spot(_result(template_id, {"a": "x"}), "a", "x")
    review_queue.resolve_field_review(i, "x")
    with pytest.raises(BulkRefusedError) as err:
        bulk_accept([i])
    assert err.value.refused == {i: "not_pending"}


def test_bulk_endpoint_reports_the_refusals(client, template_id):
    rejected = review_queue.enqueue_field_review(_result(template_id, {"a": "x"}), "a", "x", ["format_invalid"], 91.0)
    resp = client.post("/review/bulk", json={"entry_ids": [rejected]})
    assert resp.status_code == 422
    assert resp.json()["detail"]["refused"] == {str(rejected): "not_a_spot_check"}
    assert client.post("/review/bulk", json={"entry_ids": [rejected], "audit_fraction": 0.0}).status_code == 422  # never zero


# ------------------------------------------------------------------------------------------------ groups


def test_value_groups_gather_identical_reads_and_leave_out_what_needs_a_person(client, template_id):
    same = [_spot(_result(template_id, {"cur": "USD"}), "cur", "USD") for _ in range(3)]
    other = _spot(_result(template_id, {"cur": "EUR"}), "cur", "EUR")  # alone: not a group
    rejected = review_queue.enqueue_field_review(_result(template_id, {"cur": "USD"}), "cur", "USD", ["format_invalid"], 91.0)

    groups = [g for g in client.get("/review/groups", params={"by": "value", "template_id": template_id}).json()["groups"]]
    assert len(groups) == 1
    g = groups[0]
    assert g["field_id"] == "cur" and g["value"] == "USD" and g["count"] == 3 and g["entry_ids"] == same
    assert other not in g["entry_ids"] and rejected not in g["entry_ids"]


def test_document_groups_put_documents_that_need_a_person_first(client, template_id):
    only_spots = _result(template_id, {"a": "1", "b": "2"})
    _spot(only_spots, "a", "1")
    _spot(only_spots, "b", "2")
    mixed = _result(template_id, {"a": "1", "b": "2"})
    _spot(mixed, "a", "1")
    flagged = review_queue.enqueue_field_review(mixed, "b", "2", ["format_invalid"], 91.0)

    groups = [g for g in client.get("/review/groups", params={"by": "document", "template_id": template_id}).json()["groups"]]
    assert [g["result_id"] for g in groups] == [mixed, only_spots]
    assert groups[0]["needs_person"] == 1 and next(e for e in groups[0]["entries"] if e["id"] == flagged)["bulk_ok"] is False
    assert groups[1]["needs_person"] == 0 and all(e["bulk_ok"] for e in groups[1]["entries"])


def test_groups_route_is_not_read_as_a_queue_id(client):
    assert client.get("/review/groups").status_code == 200
    assert client.get("/review/groups", params={"by": "nonsense"}).status_code == 422


# ------------------------------------------------------------------------------------------------ evidence


def _evidence(client, template_id, **params):
    body = client.get("/review/evidence", params=params).json()
    return body, next((f for f in body["fields"] if f["template_id"] == template_id), None)


def test_evidence_counts_only_values_a_person_checked_one_at_a_time(client, template_id):
    seen = [_spot(_result(template_id, {"a": "x"}), "a", "x") for _ in range(10)]
    for i in seen[:9]:
        review_queue.resolve_field_review(i, "x")  # unchanged
    review_queue.resolve_field_review(seen[9], "y")  # corrected
    bulk_accepted = [_spot(_result(template_id, {"a": "x"}), "a", "x") for _ in range(5)]
    bulk_accept(bulk_accepted, audit_fraction=0.05)

    body, row = _evidence(client, template_id)
    assert row["checked"] == 10 and row["changed"] == 1  # the bulk accepts are not counted
    assert row["observed_error_rate"] == pytest.approx(0.1)
    assert row["upper_bound"] == pytest.approx(clopper_pearson_upper(1, 10))
    assert row["meets_target"] is False and row["more_clean_checks_needed"] is None
    assert body["clean_checks_needed"] == 29956


def test_evidence_says_how_many_clean_checks_are_still_needed(client, template_id):
    for _ in range(4):
        review_queue.resolve_field_review(_spot(_result(template_id, {"a": "x"}), "a", "x"), "x")
    _, row = _evidence(client, template_id, target=0.01)
    assert row["checked"] == 4 and row["changed"] == 0
    assert row["more_clean_checks_needed"] == clean_checks_needed(0.01) - 4  # 299 - 4
    assert row["meets_target"] is False


def test_whitespace_alone_is_not_a_correction(client, template_id):
    i = _spot(_result(template_id, {"a": "x y"}), "a", "x  y")
    review_queue.resolve_field_review(i, "x y")
    _, row = _evidence(client, template_id)
    assert row["changed"] == 0
