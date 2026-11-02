"""
Audited auto-approval (app/modules/trust.py): the evidence, the pipeline decision, the audit sample and the way a
wrong value withdraws the trust.

The target is loosened to 5% (59 clean checks at 95%) so the evidence is cheap to build; the maths itself is pinned
in test_review_groups.py.
"""

import uuid

import pytest

import app.config as config
from app.db import SessionLocal
from app.models.extraction_result import ExtractionResultRecord
from app.modules import review_queue, trust
from app.modules.review_groups import bulk_accept
from app.utils.stats import clean_checks_needed
from tests.test_console_api import name_field, ocr, pending_for, process  # noqa: F401  (ocr is a fixture)
from tests.test_pipeline import save_template
from tests.test_review_groups import _result, _spot

TARGET = 0.05
CLEAN = clean_checks_needed(TARGET)  # 59


@pytest.fixture(autouse=True)
def loose_target(monkeypatch):
    monkeypatch.setattr(config, "TRUST_TARGET_ERROR_RATE", TARGET)
    monkeypatch.setattr(config, "TRUST_CACHE_SECONDS", 0.0)
    monkeypatch.setattr(config, "AUDITED_AUTO_APPROVAL", True)
    monkeypatch.setattr(trust, "audit_this", lambda: False)
    trust.clear_trust_cache()
    yield
    trust.clear_trust_cache()


def _check(template_id, field_id, n, version=1, change=0, why="no_validator", bulk=False):
    """n spot checks of this field resolved by a person, `change` of them to a different value."""
    ids = []
    for i in range(n):
        rid = _result(template_id, {field_id: "x"})
        if version != 1:
            with SessionLocal() as s:
                s.get(ExtractionResultRecord, rid).template_version = version
                s.commit()
        qid = _spot(rid, field_id, "x", why=why)
        ids.append(qid)
        if not bulk:
            review_queue.resolve_field_review(qid, "WRONG" if i < change else "x")
    if bulk:
        bulk_accept(ids, audit_fraction=0.05)
    return ids


@pytest.fixture()
def tid():
    return f"trust_{uuid.uuid4().hex}"


# ------------------------------------------------------------------------------------------------ the evidence


def test_a_field_is_trusted_only_once_enough_clean_checks_exist(tid):
    _check(tid, "name", CLEAN - 1)
    assert trust.trusted_fields(tid, 1) == frozenset()
    _check(tid, "name", 1)
    assert trust.trusted_fields(tid, 1) == frozenset({"name"})


def test_one_changed_value_withdraws_the_trust_until_more_clean_checks_arrive(tid):
    _check(tid, "name", CLEAN)
    assert "name" in trust.trusted_fields(tid, 1)
    _check(tid, "name", 1, change=1)
    assert trust.trusted_fields(tid, 1) == frozenset()
    _check(tid, "name", 150)  # 1 changed in ~210: the bound is back under 5%
    assert "name" in trust.trusted_fields(tid, 1)


def test_bulk_accepts_are_not_evidence(tid):
    _check(tid, "name", CLEAN + 20, bulk=True)
    assert trust.trusted_fields(tid, 1) == frozenset()


def test_evidence_is_kept_apart_by_template_version_and_by_field(tid):
    _check(tid, "name", CLEAN, version=1)
    assert trust.trusted_fields(tid, 1) == frozenset({"name"})
    assert trust.trusted_fields(tid, 2) == frozenset()
    _check(tid, "other", 5)
    assert trust.trusted_fields(tid, 1) == frozenset({"name"})


def test_blank_optional_values_never_count_as_evidence(tid):
    _check(tid, "name", CLEAN + 5, why="blank_optional")
    assert trust.trusted_fields(tid, 1) == frozenset()


def test_the_switch_turns_it_all_off(tid, monkeypatch):
    _check(tid, "name", CLEAN)
    monkeypatch.setattr(config, "AUDITED_AUTO_APPROVAL", False)
    assert trust.trusted_fields(tid, 1) == frozenset()


# ------------------------------------------------------------------------------------------------ the pipeline


def test_a_trusted_field_is_approved_as_auto_without_a_queue_entry(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(None))  # no validator -> a spot check, however confident
    _check(unique_doc_type, "name", CLEAN)
    page = process(client, unique_doc_type, 97)

    field = page["result"]["fields"]["name"]
    assert field["approval_tier"] == "auto" and field["auto_basis"] == "trusted_field"
    assert page["spot_check_queue_ids"] == [] and pending_for(client, page["extraction_result_id"]) == []
    stored = client.get(f"/results/{page['extraction_result_id']}").json()["fields"]["name"]
    assert stored["approval_tier"] == "auto" and stored["auto_basis"] == "trusted_field"


def test_an_untrusted_field_is_still_a_spot_check(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(None))
    _check(unique_doc_type, "name", CLEAN - 1)
    page = process(client, unique_doc_type, 97)
    assert page["result"]["fields"]["name"]["approval_tier"] == "spot_check"
    assert page["result"]["fields"]["name"]["auto_basis"] is None
    assert len(page["spot_check_queue_ids"]) == 1


def test_a_random_share_of_a_trusted_field_still_goes_to_a_person(client, unique_doc_type, ocr, monkeypatch):
    save_template(client, unique_doc_type, name_field(None))
    _check(unique_doc_type, "name", CLEAN)
    monkeypatch.setattr(trust, "audit_this", lambda: True)
    page = process(client, unique_doc_type, 97)

    assert page["result"]["fields"]["name"]["approval_tier"] == "spot_check"
    [entry] = pending_for(client, page["extraction_result_id"])
    assert entry["entry_type"] == "spot_check" and entry["payload"]["audit"] is True


def test_a_value_module_7_rejected_is_never_trusted_into_auto(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(r"^\d+$"))  # "Some Name" fails the regex -> review
    _check(unique_doc_type, "name", CLEAN)
    page = process(client, unique_doc_type, 97, r"^\d+$")
    assert page["result"]["fields"]["name"]["approval_tier"] == "review"
    assert len(page["field_review_queue_ids"]) == 1


def test_correcting_a_trusted_auto_value_withdraws_the_trust(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(None))
    _check(unique_doc_type, "name", CLEAN)
    page = process(client, unique_doc_type, 97)
    rid = page["extraction_result_id"]
    assert "name" in trust.trusted_fields(unique_doc_type, 1)

    assert client.patch(f"/results/{rid}/fields/name", json={"value": "Someone Else"}).status_code == 200
    assert trust.trusted_fields(unique_doc_type, 1) == frozenset()  # that was a wrong value approved unseen
    assert client.get(f"/results/{rid}").json()["fields"]["name"]["auto_basis"] is None
    # and the next document goes back to a person
    assert process(client, unique_doc_type, 97)["result"]["fields"]["name"]["approval_tier"] == "spot_check"


def test_whitespace_only_difference_is_not_an_error(client, unique_doc_type, ocr):
    save_template(client, unique_doc_type, name_field(None))
    _check(unique_doc_type, "name", CLEAN)
    rid = process(client, unique_doc_type, 97)["extraction_result_id"]
    client.patch(f"/results/{rid}/fields/name", json={"value": "Some  Name"})  # "Some Name" with a double space
    assert "name" in trust.trusted_fields(unique_doc_type, 1)


def test_evidence_endpoint_reports_the_trust(client, tid):
    _check(tid, "name", CLEAN)
    body = client.get("/review/evidence").json()
    row = next(f for f in body["fields"] if f["template_id"] == tid)
    assert row["version"] == 1 and row["checked"] == CLEAN and row["meets_target"] is True
    assert body["audited_auto_approval"] is True
