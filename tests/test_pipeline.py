import io
from unittest.mock import Mock

from PIL import Image

import app.modules.field_extraction as field_extraction
import app.modules.type_identification as type_identification
from app.db import SessionLocal
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord


def make_image_bytes(size: tuple[int, int] = (20, 20)) -> bytes:
    img = Image.new("RGB", size, color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def save_template(client, template_id, fields):
    resp = client.post(
        "/templates",
        json={
            "template_id": template_id,
            "anchors": [{"text": "HEADER", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85}],
            "fields": fields,
        },
    )
    assert resp.status_code == 201
    return resp.json()


def test_clean_match_produces_complete_result(client, unique_doc_type, monkeypatch):
    template = save_template(
        client,
        unique_doc_type,
        [
            {
                "id": "name",
                "label": "Name",
                "bbox": [0.1, 0.3, 0.5, 0.4],
                "data_type": "string",
                "validation_regex": r"^[A-Za-z ]+$",
                "confidence_floor": 0.5,
            }
        ],
    )

    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(
        field_extraction.pytesseract, "image_to_data", Mock(return_value={"text": ["John", "Doe"], "conf": [95, 92]})
    )

    resp = client.post(
        "/documents/process",
        files={"file": ("sample.jpg", make_image_bytes(), "image/jpeg")},
        data={"type_hint": unique_doc_type},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["page_count"] == 1
    page = body["pages"][0]
    assert page["confirmed"] is True
    assert page["reason"] is None
    assert page["result"]["template_id"] == unique_doc_type
    assert page["result"]["version"] == template["version"]
    assert page["result"]["overall_status"] == "complete"
    assert page["field_review_queue_ids"] == []

    session = SessionLocal()
    try:
        row = session.get(ExtractionResultRecord, page["extraction_result_id"])
        assert row is not None
        assert row.overall_status == "complete"
        assert row.fields["name"]["value"] == "John Doe"
    finally:
        session.close()


def test_unmatched_hint_rejects_and_enqueues_new_template_needed(client, unique_doc_type):
    # unique_doc_type is never saved anywhere - a guaranteed-unresolvable hint.
    resp = client.post(
        "/documents/process",
        files={"file": ("sample.jpg", make_image_bytes(), "image/jpeg")},
        data={"type_hint": unique_doc_type},
    )

    assert resp.status_code == 200
    page = resp.json()["pages"][0]
    assert page["confirmed"] is False
    assert page["reason"] == "unknown_type_hint"
    assert page["extraction_result_id"] is None
    assert page["review_queue_id"] is not None

    session = SessionLocal()
    try:
        row = session.get(ReviewQueueRecord, page["review_queue_id"])
        assert row is not None
        assert row.entry_type == "new_template_needed"
        assert row.status == "pending"
        assert row.payload["attempted_type_hint"] == unique_doc_type
        assert row.payload["rejection_reason"] == "unknown_type_hint"
    finally:
        session.close()


def test_one_flagged_field_enqueues_exactly_one_field_review(client, unique_doc_type, monkeypatch):
    save_template(
        client,
        unique_doc_type,
        [
            {
                "id": "good_field",
                "label": "Good",
                "bbox": [0.1, 0.2, 0.5, 0.3],
                "data_type": "string",
                "validation_regex": r"^[A-Za-z ]+$",
                "confidence_floor": 0.5,
            },
            {
                "id": "bad_field",
                "label": "Bad",
                "bbox": [0.1, 0.4, 0.5, 0.5],
                "data_type": "string",
                "validation_regex": r"^[0-9]+$",  # will fail against non-digit OCR text below
                "confidence_floor": 0.5,
            },
        ],
    )

    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(
        field_extraction.pytesseract,
        "image_to_data",
        Mock(
            side_effect=[
                {"text": ["Valid", "Name"], "conf": [95, 95]},  # good_field
                {"text": ["not", "digits"], "conf": [95, 95]},  # bad_field
            ]
        ),
    )

    resp = client.post(
        "/documents/process",
        files={"file": ("sample.jpg", make_image_bytes(), "image/jpeg")},
        data={"type_hint": unique_doc_type},
    )

    assert resp.status_code == 200
    page = resp.json()["pages"][0]
    assert page["confirmed"] is True
    assert page["result"]["overall_status"] == "needs_review"
    assert page["result"]["fields"]["good_field"]["needs_review"] is False
    assert page["result"]["fields"]["bad_field"]["needs_review"] is True
    assert len(page["field_review_queue_ids"]) == 1

    session = SessionLocal()
    try:
        row = session.get(ReviewQueueRecord, page["field_review_queue_ids"][0])
        assert row.entry_type == "field_review"
        assert row.payload["field_id"] == "bad_field"
        assert row.payload["extraction_result_id"] == page["extraction_result_id"]
    finally:
        session.close()


def test_multiple_flagged_fields_enqueue_one_review_each(client, unique_doc_type, monkeypatch):
    save_template(
        client,
        unique_doc_type,
        [
            {
                "id": "field_a",
                "label": "A",
                "bbox": [0.1, 0.2, 0.5, 0.3],
                "data_type": "string",
                "validation_regex": r"^[0-9]+$",
                "confidence_floor": 0.5,
            },
            {
                "id": "field_b",
                "label": "B",
                "bbox": [0.1, 0.4, 0.5, 0.5],
                "data_type": "string",
                "validation_regex": r"^[0-9]+$",
                "confidence_floor": 0.5,
            },
        ],
    )

    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(
        field_extraction.pytesseract,
        "image_to_data",
        Mock(return_value={"text": ["not", "digits"], "conf": [95, 95]}),  # fails both fields identically
    )

    resp = client.post(
        "/documents/process",
        files={"file": ("sample.jpg", make_image_bytes(), "image/jpeg")},
        data={"type_hint": unique_doc_type},
    )

    assert resp.status_code == 200
    page = resp.json()["pages"][0]
    assert page["confirmed"] is True
    assert page["result"]["overall_status"] == "needs_review"
    assert len(page["field_review_queue_ids"]) == 2

    session = SessionLocal()
    try:
        rows = [session.get(ReviewQueueRecord, qid) for qid in page["field_review_queue_ids"]]
        assert all(r.entry_type == "field_review" for r in rows)
        assert all(r.payload["extraction_result_id"] == page["extraction_result_id"] for r in rows)
        assert {r.payload["field_id"] for r in rows} == {"field_a", "field_b"}
    finally:
        session.close()
