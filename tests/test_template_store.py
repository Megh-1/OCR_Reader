import copy
import threading

import pytest

from app.db import SessionLocal
from app.models.template import TemplateRecord


@pytest.fixture()
def valid_payload(unique_doc_type):
    return {
        "template_id": unique_doc_type,
        "anchors": [
            {"text": "BITS PILANI", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85},
        ],
        "fields": [
            {
                "id": "student_name",
                "label": "Student Name",
                "bbox": [0.05, 0.30, 0.55, 0.38],
                "data_type": "string",
                "validation_regex": "^[A-Za-z ]{2,60}$",
                "confidence_floor": 0.75,
            }
        ],
    }


def test_valid_template_persists_with_version_1(client, valid_payload):
    resp = client.post("/templates", json=valid_payload)
    assert resp.status_code == 201
    body = resp.json()
    assert body["template_id"] == valid_payload["template_id"]
    assert body["version"] == 1
    assert len(body["anchors"]) == 1


def test_saving_same_doc_type_again_creates_version_2_not_update(client, valid_payload):
    first = client.post("/templates", json=valid_payload)
    second = client.post("/templates", json=valid_payload)

    assert first.json()["version"] == 1
    assert second.json()["version"] == 2

    # Prove it's a new row, not an in-place update: both versions must
    # still independently exist afterward, with the original untouched.
    session = SessionLocal()
    try:
        rows = (
            session.query(TemplateRecord)
            .filter_by(doc_type=valid_payload["template_id"])
            .order_by(TemplateRecord.version)
            .all()
        )
        assert [r.version for r in rows] == [1, 2]
        assert rows[0].config["version"] == 1
    finally:
        session.close()


def test_bbox_outside_unit_range_is_rejected(client, valid_payload):
    payload = copy.deepcopy(valid_payload)
    payload["anchors"][0]["bbox"] = [0.05, 0.02, 1.5, 0.08]
    resp = client.post("/templates", json=payload)
    assert resp.status_code == 422


def test_bbox_x2_less_than_x1_is_rejected(client, valid_payload):
    payload = copy.deepcopy(valid_payload)
    payload["anchors"][0]["bbox"] = [0.45, 0.02, 0.05, 0.08]
    resp = client.post("/templates", json=payload)
    assert resp.status_code == 422


def test_zero_anchors_is_rejected(client, valid_payload):
    payload = copy.deepcopy(valid_payload)
    payload["anchors"] = []
    resp = client.post("/templates", json=payload)
    assert resp.status_code == 422


def test_concurrent_posts_for_same_new_doc_type_do_not_both_get_version_1(client, valid_payload):
    results: list = []
    barrier = threading.Barrier(2)

    def do_post():
        barrier.wait()  # maximize the chance both requests actually overlap
        results.append(client.post("/templates", json=valid_payload))

    threads = [threading.Thread(target=do_post) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(r.status_code == 201 for r in results), [r.text for r in results]
    versions = sorted(r.json()["version"] for r in results)
    assert versions == [1, 2]
