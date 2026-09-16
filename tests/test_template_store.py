import copy
import threading

import pytest

from app.db import SessionLocal
from app.models.template import TemplateRecord
from app.modules.template_store import get_latest_template, get_template_version, list_known_doc_types


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


def _save_n_distinguishable_versions(client, valid_payload, n):
    """Saves n versions of the same doc_type, each with a distinct anchor
    text ("v1", "v2", ...) so a test can tell which version it actually got
    back, not just that some version came back."""
    for i in range(1, n + 1):
        payload = copy.deepcopy(valid_payload)
        payload["anchors"][0]["text"] = f"v{i}"
        resp = client.post("/templates", json=payload)
        assert resp.status_code == 201
        assert resp.json()["version"] == i


# --- Module 4: read/query access layer --------------------------------------


def test_get_latest_template_returns_highest_version_fully_deserialized(client, valid_payload):
    doc_type = valid_payload["template_id"]
    _save_n_distinguishable_versions(client, valid_payload, 3)

    result = get_latest_template(doc_type)

    assert result is not None
    assert result.version == 3
    assert result.template_id == doc_type
    assert result.anchors[0].text == "v3"
    assert result.fields[0].label == valid_payload["fields"][0]["label"]


def test_get_latest_template_returns_none_for_unknown_doc_type(unique_doc_type):
    assert get_latest_template(unique_doc_type) is None


def test_get_template_version_returns_exact_version_not_latest(client, valid_payload):
    doc_type = valid_payload["template_id"]
    _save_n_distinguishable_versions(client, valid_payload, 3)

    result = get_template_version(doc_type, 1)

    assert result is not None
    assert result.version == 1
    assert result.anchors[0].text == "v1"  # not "v3" - would prove an "always latest" bug


def test_get_template_version_returns_none_for_unknown_version(client, valid_payload):
    doc_type = valid_payload["template_id"]
    _save_n_distinguishable_versions(client, valid_payload, 3)

    result = get_template_version(doc_type, 99)

    assert result is None


def test_list_known_doc_types_returns_each_doc_type_once(client, valid_payload):
    doc_type = valid_payload["template_id"]
    _save_n_distinguishable_versions(client, valid_payload, 3)

    doc_types = list_known_doc_types()

    assert doc_types.count(doc_type) == 1


def test_get_endpoint_404s_for_unknown_doc_type(client, unique_doc_type):
    resp = client.get(f"/templates/{unique_doc_type}")
    assert resp.status_code == 404


def test_get_version_endpoint_404s_for_unknown_version(client, valid_payload):
    doc_type = valid_payload["template_id"]
    _save_n_distinguishable_versions(client, valid_payload, 1)

    resp = client.get(f"/templates/{doc_type}/versions/99")

    assert resp.status_code == 404
