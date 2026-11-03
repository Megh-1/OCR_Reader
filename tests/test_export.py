"""The batch JSON export (app/modules/export.py): provenance per value, and unsettled values withheld in strict mode."""

import random
import uuid
from datetime import datetime, timezone

import pytest

from app.db import SessionLocal
from app.models.batch import BatchDocumentRecord, BatchRecord
from app.models.extraction_result import ExtractionResultRecord
from app.modules import review_queue
from app.modules.review_groups import bulk_accept


def _field(value, tier, needs_review=False):
    return {"value": value, "confidence": 80.0, "needs_review": needs_review, "approval_tier": tier, "reasons": ["format_invalid"] if needs_review else []}


def _result(template_id, fields):
    with SessionLocal() as session:
        row = ExtractionResultRecord(template_id=template_id, template_version=1, extracted_at=datetime.now(timezone.utc), overall_status="needs_review", fields={k: {"field_id": k, **v} for k, v in fields.items()})
        session.add(row)
        session.commit()
        return row.id


def _batch(pages):
    batch_id = uuid.uuid4().hex
    with SessionLocal() as session:
        session.add(BatchRecord(id=batch_id, created_at=datetime.now(timezone.utc), type_hint=None))
        session.flush()
        session.add(BatchDocumentRecord(batch_id=batch_id, position=0, filename="a.pdf", status="completed", attempts=1, result={"pages": pages}, created_at=datetime.now(timezone.utc)))
        session.commit()
    return batch_id


@pytest.fixture()
def template_id():
    return f"exp_{uuid.uuid4().hex}"


def test_export_says_who_vouches_for_each_value_and_withholds_the_rest(client, template_id):
    rid = _result(template_id, {
        "auto_f": _field("A", "auto"),
        "person_f": _field("P", "spot_check"),
        "open_f": _field("O", "spot_check"),
        "bad_f": _field("X", "review", needs_review=True),
    })
    person = review_queue.enqueue_spot_check(rid, "person_f", "P", 80.0, "no_validator")
    review_queue.resolve_field_review(person, "P2")  # corrected on its own
    batch_id = _batch([{"extraction_result_id": rid, "reader": "vlm"}, {"confirmed": False, "reason": "no_match"}])

    body = client.get(f"/documents/batch/{batch_id}/export").json()
    page = body["documents"][0]["pages"][0]
    fields = page["fields"]
    assert fields["auto_f"] == {"value": "A", "status": "auto", "confidence": 80.0}
    assert fields["person_f"]["status"] == "confirmed" and fields["person_f"]["value"] == "P2"
    assert fields["open_f"]["status"] == "unverified" and fields["open_f"]["value"] is None and fields["open_f"]["suggested"] == "O"
    assert fields["bad_f"]["value"] is None and fields["bad_f"]["suggested"] == "X" and fields["bad_f"]["reasons"] == ["format_invalid"]
    assert page["complete"] is False
    assert body["documents"][0]["pages"][1] == {"recognised": False, "reason": "no_match"}
    s = body["summary"]
    assert s["fields"] == 4 and s["withheld"] == 2 and s["emitted"] == 2 and s["unrecognised_pages"] == 1
    assert s["emitted_basis"] == {"confirmed": 1, "accepted": 0, "auto": 1, "unverified": 2}


def test_a_bulk_accept_is_marked_accepted_and_non_strict_keeps_every_read(client, template_id):
    r1, r2 = (_result(template_id, {"f": _field("V", "spot_check")}) for _ in range(2))
    ids = [review_queue.enqueue_spot_check(r, "f", "V", 80.0, "no_validator") for r in (r1, r2)]
    out = bulk_accept(ids, audit_fraction=0.05, rng=random.Random(1))
    accepted_result = r1 if out["accepted"][0] == ids[0] else r2
    other_result = r2 if accepted_result == r1 else r1
    batch_id = _batch([{"extraction_result_id": accepted_result}, {"extraction_result_id": other_result}])

    strict = client.get(f"/documents/batch/{batch_id}/export").json()["documents"][0]["pages"]
    assert strict[0]["fields"]["f"]["status"] == "accepted" and strict[0]["fields"]["f"]["value"] == "V"
    assert strict[1]["fields"]["f"]["status"] == "unverified" and strict[1]["fields"]["f"]["value"] is None  # the audit sample, still unseen

    loose = client.get(f"/documents/batch/{batch_id}/export", params={"strict": "false"}).json()
    assert loose["documents"][0]["pages"][1]["fields"]["f"]["value"] == "V"
    assert loose["summary"]["withheld"] == 0


def test_a_correction_on_the_results_screen_counts_as_a_person_checking(client, template_id):
    rid = _result(template_id, {"f": _field("wrong", "spot_check")})
    assert client.patch(f"/results/{rid}/fields/f", json={"value": "right"}).status_code == 200
    batch_id = _batch([{"extraction_result_id": rid}])
    f = client.get(f"/documents/batch/{batch_id}/export").json()["documents"][0]["pages"][0]["fields"]["f"]
    assert f["status"] == "confirmed" and f["value"] == "right"


def test_export_of_an_unknown_batch_is_404(client):
    assert client.get("/documents/batch/nope/export").status_code == 404
