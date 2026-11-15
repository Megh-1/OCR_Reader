"""
The API surface the console UI reads: spot checks in the review queue, a template's
version history, the recent-results list and the recent-batches list, and the Results
screen's direct field correction.
"""

from unittest.mock import Mock

import fakeredis
import pytest

import app.job_queue as job_queue
import app.modules.field_extraction as field_extraction
import app.modules.type_identification as type_identification
import app.modules.validation as validation
from tests.test_field_extraction import ocr_responses
from tests.test_pipeline import make_image_bytes, save_template


def process(client, doc_type, confidence, regex=None):
    """One field per template (see test_approval_tiers), so mocked reads cannot interleave.
    `regex` is the field's own, so a digit-only one gets its extra whitelisted read mocked too."""
    response = ("string", {"text": ["Some", "Name"], "conf": [confidence, confidence]}) + ((regex,) if regex else ())
    field_extraction.pytesseract.image_to_data = Mock(side_effect=ocr_responses(response))
    resp = client.post(
        "/documents/process",
        files={"file": ("sample.jpg", make_image_bytes(), "image/jpeg")},
        data={"type_hint": doc_type},
    )
    assert resp.status_code == 200
    return resp.json()["pages"][0]


@pytest.fixture()
def ocr(monkeypatch):
    monkeypatch.setattr(validation, "CONFIDENCE_AUTO_APPROVE_THRESHOLD", 0.80)
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", Mock())


def name_field(regex):
    return [{"id": "name", "label": "N", "bbox": [0.1, 0.2, 0.5, 0.3], "data_type": "string",
             "validation_regex": regex, "confidence_floor": 0.5}]


def pending_for(client, result_id):
    return [e for e in client.get("/review").json() if e["payload"].get("extraction_result_id") == result_id]


# --- spot checks in the review queue ---------------------------------------------------------


def test_an_accepted_field_under_the_auto_threshold_is_enqueued_as_a_spot_check(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    page = process(client, unique_doc_type, 66)

    assert page["result"]["overall_status"] == "complete"  # a spot check never holds the record back
    assert len(page["spot_check_queue_ids"]) == 1 and page["field_review_queue_ids"] == []
    [entry] = pending_for(client, page["extraction_result_id"])
    assert entry["entry_type"] == "spot_check"
    assert entry["payload"]["why"] == "below_auto_threshold" and entry["payload"]["confidence"] == 66


def test_a_confident_field_with_no_validator_is_a_spot_check_saying_so(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(None))
    page = process(client, unique_doc_type, 97)

    [entry] = pending_for(client, page["extraction_result_id"])
    assert entry["entry_type"] == "spot_check" and entry["payload"]["why"] == "no_validator"


def test_an_auto_field_is_not_enqueued_at_all(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    page = process(client, unique_doc_type, 97)

    assert page["spot_check_queue_ids"] == [] and pending_for(client, page["extraction_result_id"]) == []


def test_a_flagged_field_records_its_confidence(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^\d+$"))
    page = process(client, unique_doc_type, 97, regex=r"^\d+$")

    [entry] = pending_for(client, page["extraction_result_id"])
    assert entry["entry_type"] == "field_review" and entry["payload"]["confidence"] == 97


def test_resolving_a_spot_check_writes_the_corrected_value(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    page = process(client, unique_doc_type, 66)
    queue_id = page["spot_check_queue_ids"][0]

    resp = client.patch(f"/review/{queue_id}", json={"confirmed_value": "Some Names"})

    assert resp.status_code == 200
    stored = client.get(f"/results/{page['extraction_result_id']}").json()
    assert stored["fields"]["name"]["value"] == "Some Names"
    assert stored["overall_status"] == "complete"
    assert pending_for(client, page["extraction_result_id"]) == []


def test_a_spot_check_needs_a_confirmed_value(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    queue_id = process(client, unique_doc_type, 66)["spot_check_queue_ids"][0]
    assert client.patch(f"/review/{queue_id}", json={}).status_code == 422


# --- template version history ----------------------------------------------------------------


def test_versions_are_listed_newest_first(client, unique_doc_type):
    save_template(client, unique_doc_type, name_field(None))
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$") + name_field(None)[:0])

    versions = client.get(f"/templates/{unique_doc_type}/versions").json()

    assert [v["version"] for v in versions] == [2, 1]
    assert versions[0]["field_count"] == 1 and versions[0]["anchor_count"] == 1 and versions[0]["is_archived"] is False


def test_versions_of_an_unknown_template_is_404(client):
    assert client.get("/templates/no_such_doc_type_xyz/versions").status_code == 404


# --- recent results --------------------------------------------------------------------------


def test_results_are_listed_newest_first_with_tier_counts(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    first = process(client, unique_doc_type, 97)["extraction_result_id"]
    second = process(client, unique_doc_type, 66)["extraction_result_id"]

    listed = client.get("/results", params={"template_id": unique_doc_type}).json()

    assert [r["id"] for r in listed] == [second, first]
    assert listed[0]["tier_counts"] == {"auto": 0, "spot_check": 1, "review": 0, "unknown": 0}
    assert listed[1]["tier_counts"]["auto"] == 1 and listed[1]["has_image"] is True
    assert listed[0]["template_id"] == unique_doc_type and listed[0]["field_count"] == 1


def test_the_results_limit_is_bounded(client):
    assert client.get("/results", params={"limit": 0}).status_code == 422
    assert client.get("/results", params={"limit": 201}).status_code == 422


# --- recent batches --------------------------------------------------------------------------


def test_batches_are_listed_with_their_document_counts(client):
    job_queue.set_connection(fakeredis.FakeStrictRedis(server=fakeredis.FakeServer()))
    try:
        files = [("files", (f"{i}.jpg", make_image_bytes(), "image/jpeg")) for i in range(3)]
        batch_id = client.post("/documents/batch", files=files).json()["batch_id"]
        listed = client.get("/documents/batch").json()
    finally:
        job_queue.set_connection(None)

    mine = next(b for b in listed if b["batch_id"] == batch_id)
    assert listed[0]["batch_id"] == batch_id  # newest first
    assert mine["document_count"] == 3 and mine["counts"]["queued"] == 3 and mine["status"] == "queued"


# --- Results screen: direct field correction -------------------------------------------------


def test_correcting_a_field_writes_the_value_and_is_reflected_on_read(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    page = process(client, unique_doc_type, 97)  # auto - no queue entry, nothing pending
    result_id = page["extraction_result_id"]

    resp = client.patch(f"/results/{result_id}/fields/name", json={"value": "A Better Name"})

    assert resp.status_code == 200
    assert resp.json() == {
        "field_id": "name", "value": "A Better Name", "confidence": 97.0,
        "needs_review": False, "reasons": [], "approval_tier": "auto", "auto_basis": None, "verified_by": "person",
        "engines": None, "source_text": None, "grounded": None, "agreed": None,  # only a page-engine read carries these
    }
    assert client.get(f"/results/{result_id}").json()["fields"]["name"]["value"] == "A Better Name"


def test_correcting_a_flagged_field_resolves_its_pending_review_entry_and_completes_the_result(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^\d+$"))  # "Some Name" will never match -> review
    page = process(client, unique_doc_type, 97, regex=r"^\d+$")
    result_id = page["extraction_result_id"]
    assert page["result"]["overall_status"] == "needs_review"
    [queue_id] = page["field_review_queue_ids"]

    resp = client.patch(f"/results/{result_id}/fields/name", json={"value": "42"})

    assert resp.status_code == 200 and resp.json()["needs_review"] is False
    assert client.get(f"/results/{result_id}").json()["overall_status"] == "complete"
    assert [e for e in client.get("/review").json() if e["id"] == queue_id] == []  # resolved, not pending


def test_correcting_a_spot_check_field_resolves_its_pending_entry_too(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^[A-Za-z ]+$"))
    page = process(client, unique_doc_type, 66)  # under the 0.80 test threshold -> spot_check
    result_id = page["extraction_result_id"]
    [queue_id] = page["spot_check_queue_ids"]

    resp = client.patch(f"/results/{result_id}/fields/name", json={"value": "Some Names"})

    assert resp.status_code == 200
    assert [e for e in client.get("/review").json() if e["id"] == queue_id] == []


def test_correcting_an_unknown_result_is_404(client):
    assert client.patch("/results/999999/fields/name", json={"value": "x"}).status_code == 404


def test_correcting_an_unknown_field_on_a_real_result_is_404(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(None))
    result_id = process(client, unique_doc_type, 97)["extraction_result_id"]

    resp = client.patch(f"/results/{result_id}/fields/no_such_field", json={"value": "x"})
    assert resp.status_code == 404


def test_a_correction_needs_a_value(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(None))
    result_id = process(client, unique_doc_type, 97)["extraction_result_id"]

    assert client.patch(f"/results/{result_id}/fields/name", json={}).status_code == 422


# --------------------------------------------------------------------------- rule editor: suggestions and a dry run


def _store_results(template_id, rows):
    from datetime import datetime, timezone

    from app.db import SessionLocal
    from app.models.extraction_result import ExtractionResultRecord

    with SessionLocal() as session:
        for values in rows:
            session.add(ExtractionResultRecord(
                template_id=template_id, template_version=1, extracted_at=datetime.now(timezone.utc), overall_status="complete",
                fields={k: {"value": v, "confidence": 99.0, "needs_review": False, "approval_tier": "auto", "reasons": []} for k, v in values.items()},
            ))
        session.commit()


def test_rule_editor_suggests_then_dry_runs_a_rule(client, unique_doc_type):
    _store_results(unique_doc_type, [
        {"qty": "4", "price": "2.50", "total": "10.00"},
        {"qty": "3", "price": "5.00", "total": "15.00"},
        {"qty": "7", "price": "1.00", "total": "7.00"},
    ])
    found = client.post("/templates/suggest-checks", json={"template_id": unique_doc_type}).json()
    assert found["documents"] == 3
    product = next(s for s in found["suggestions"] if s["kind"] == "product" and s["target"] == "total")
    assert product["documents"] == 3

    # a rule that does not hold is reported with examples, so a person sees it before adding it
    bad = {"id": "wrong", "label": "qty + price = total", "kind": "sum", "inputs": ["qty", "price"], "target": "total"}
    tested = client.post("/templates/test-check", json={"template_id": unique_doc_type, "check": bad}).json()
    assert tested["failed"] == 3 and tested["passed"] == 0 and len(tested["failures"]) == 3
    good = {k: v for k, v in product.items() if k != "documents"}
    assert client.post("/templates/test-check", json={"template_id": unique_doc_type, "check": good}).json()["passed"] == 3


def test_suggest_needs_at_least_two_documents(client, unique_doc_type):
    _store_results(unique_doc_type, [{"a": "1", "b": "2"}])
    assert client.post("/templates/suggest-checks", json={"template_id": unique_doc_type}).json()["suggestions"] == []
