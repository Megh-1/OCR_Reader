import pytest

from app.db import SessionLocal
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord
from app.modules.output_compiler import compile_output, persist_result
from app.modules.review_queue import (
    ReviewTypeMismatchError,
    TemplateNotFoundError,
    enqueue_field_review,
    enqueue_new_template_needed,
    list_pending,
    resolve_field_review,
    resolve_new_template_needed,
)
from app.modules.validation import FieldValidationResult, ValidationSummary


def make_validation_result(field_id, value, confidence, accepted, reasons=None) -> FieldValidationResult:
    return FieldValidationResult(field_id=field_id, value=value, confidence=confidence, accepted=accepted, reasons=reasons or [])


def make_flagged_extraction_result(unique_doc_type, field_id="name", value="J0hn D0e", confidence=40.0) -> int:
    summary = ValidationSummary(
        accepted={},
        flagged={field_id: make_validation_result(field_id, value=value, confidence=confidence, accepted=False, reasons=["low_confidence"])},
    )
    compiled = compile_output(unique_doc_type, 1, summary)
    return persist_result(compiled)


def test_overall_status_only_flips_to_complete_once_all_flagged_fields_are_resolved(unique_doc_type):
    summary = ValidationSummary(
        accepted={},
        flagged={
            "name": make_validation_result("name", value="J0hn", confidence=40.0, accepted=False, reasons=["low_confidence"]),
            "code": make_validation_result("code", value="Z9", confidence=30.0, accepted=False, reasons=["format_invalid"]),
        },
    )
    compiled = compile_output(unique_doc_type, 1, summary)
    result_id = persist_result(compiled)

    name_queue_id = enqueue_field_review(result_id, "name", "J0hn", ["low_confidence"])
    code_queue_id = enqueue_field_review(result_id, "code", "Z9", ["format_invalid"])

    session = SessionLocal()
    try:
        assert session.get(ExtractionResultRecord, result_id).overall_status == "needs_review"
    finally:
        session.close()

    # Resolve only the first flagged field - the second is still flagged,
    # so the result as a whole must stay needs_review.
    resolve_field_review(name_queue_id, "John")

    session = SessionLocal()
    try:
        row = session.get(ExtractionResultRecord, result_id)
        assert row.fields["name"]["needs_review"] is False
        assert row.fields["code"]["needs_review"] is True  # untouched
        assert row.overall_status == "needs_review"
    finally:
        session.close()

    # Resolve the second and last flagged field - now nothing in this
    # result is still flagged, so overall_status flips to complete, done
    # in the same transaction as this write-back.
    resolve_field_review(code_queue_id, "O+")

    session = SessionLocal()
    try:
        row = session.get(ExtractionResultRecord, result_id)
        assert row.fields["name"]["needs_review"] is False
        assert row.fields["code"]["needs_review"] is False
        assert row.overall_status == "complete"
    finally:
        session.close()


def test_resolve_field_review_actually_updates_extraction_result(unique_doc_type):
    result_id = make_flagged_extraction_result(unique_doc_type)
    queue_id = enqueue_field_review(result_id, "name", "J0hn D0e", ["low_confidence"])

    resolve_field_review(queue_id, "John Doe")

    # Query back independently - not trusting the function calls alone.
    session = SessionLocal()
    try:
        result_row = session.get(ExtractionResultRecord, result_id)
        assert result_row.fields["name"]["value"] == "John Doe"
        assert result_row.fields["name"]["needs_review"] is False

        queue_row = session.get(ReviewQueueRecord, queue_id)
        assert queue_row.status == "resolved"
        assert queue_row.resolved_at is not None
        assert queue_row.resolution["confirmed_value"] == "John Doe"
    finally:
        session.close()


def test_resolve_field_review_rejects_wrong_entry_type():
    queue_id = enqueue_new_template_needed(attempted_type_hint="some_hint", rejection_reason="no_match")

    with pytest.raises(ReviewTypeMismatchError):
        resolve_field_review(queue_id, "some value")

    session = SessionLocal()
    try:
        row = session.get(ReviewQueueRecord, queue_id)
        assert row.status == "pending"  # not silently misapplied
        assert row.resolution is None
    finally:
        session.close()


def test_resolve_new_template_needed_with_existing_template_succeeds(client, unique_doc_type):
    resp = client.post(
        "/templates",
        json={
            "template_id": unique_doc_type,
            "anchors": [{"text": "X", "bbox": [0.01, 0.01, 0.2, 0.05], "match_threshold": 0.5}],
            "fields": [],
        },
    )
    assert resp.status_code == 201

    queue_id = enqueue_new_template_needed(attempted_type_hint=None, rejection_reason="unknown_type_hint")

    resolve_new_template_needed(queue_id, unique_doc_type)

    session = SessionLocal()
    try:
        row = session.get(ReviewQueueRecord, queue_id)
        assert row.status == "resolved"
        assert row.resolution["created_template_id"] == unique_doc_type
    finally:
        session.close()


def test_resolve_new_template_needed_rejects_nonexistent_template(unique_doc_type):
    # unique_doc_type here is deliberately never saved anywhere.
    queue_id = enqueue_new_template_needed(attempted_type_hint=None, rejection_reason="no_match")

    with pytest.raises(TemplateNotFoundError):
        resolve_new_template_needed(queue_id, unique_doc_type)

    session = SessionLocal()
    try:
        row = session.get(ReviewQueueRecord, queue_id)
        assert row.status == "pending"  # proves the existence check is real, not decorative
        assert row.resolution is None
    finally:
        session.close()


def test_list_pending_filters_by_entry_type(unique_doc_type):
    marker = unique_doc_type
    fr_id = enqueue_field_review(999999, f"field_{marker}", "orig", ["low_confidence"])
    nt_id = enqueue_new_template_needed(attempted_type_hint=marker, rejection_reason="no_match")

    all_entries = list_pending()
    all_ids = {e.id for e in all_entries}
    assert fr_id in all_ids
    assert nt_id in all_ids

    field_review_entries = list_pending(entry_type="field_review")
    field_review_ids = {e.id for e in field_review_entries}
    assert fr_id in field_review_ids
    assert nt_id not in field_review_ids
    assert all(e.entry_type == "field_review" for e in field_review_entries)

    new_template_entries = list_pending(entry_type="new_template_needed")
    new_template_ids = {e.id for e in new_template_entries}
    assert nt_id in new_template_ids
    assert fr_id not in new_template_ids
    assert all(e.entry_type == "new_template_needed" for e in new_template_entries)


def test_patch_dispatches_field_review_without_caller_specifying_type(client, unique_doc_type):
    result_id = make_flagged_extraction_result(unique_doc_type, field_id="name", value="bad")
    queue_id = enqueue_field_review(result_id, "name", "bad", ["low_confidence"])

    resp = client.patch(f"/review/{queue_id}", json={"confirmed_value": "Good Value"})

    assert resp.status_code == 200
    assert resp.json() == {"id": queue_id, "status": "resolved"}

    session = SessionLocal()
    try:
        result_row = session.get(ExtractionResultRecord, result_id)
        assert result_row.fields["name"]["value"] == "Good Value"
        assert result_row.fields["name"]["needs_review"] is False
    finally:
        session.close()


def test_patch_dispatches_new_template_needed_without_caller_specifying_type(client, unique_doc_type):
    resp = client.post(
        "/templates",
        json={
            "template_id": unique_doc_type,
            "anchors": [{"text": "X", "bbox": [0.01, 0.01, 0.2, 0.05], "match_threshold": 0.5}],
            "fields": [],
        },
    )
    assert resp.status_code == 201
    queue_id = enqueue_new_template_needed(attempted_type_hint=None, rejection_reason="no_match")

    resp = client.patch(f"/review/{queue_id}", json={"created_template_id": unique_doc_type})

    assert resp.status_code == 200
    assert resp.json() == {"id": queue_id, "status": "resolved"}
