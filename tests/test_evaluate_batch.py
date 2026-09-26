"""scripts/evaluate_batch.py: turning what the batch API returns into the harness's own result objects."""

from pathlib import Path

from scripts.evaluate_accuracy import OUTCOME_AMBIGUOUS, OUTCOME_CORRECT, OUTCOME_NEW_TEMPLATE, OUTCOME_WRONG
from scripts.evaluate_batch import _seconds, _to_file_result, _validated_fields

EXPECTED = {"template_id": "air_waybill", "tier": "camera_photo_jpeg", "fields": {"carrier": "EMIRATES", "consignee_name": "ACME TRADERS"}}
NEVER_READ = Path("does-not-exist.jpg")  # the tier is given, so the file is not opened


def completed(fields, confirmed=True, template="air_waybill", reason=None, text_native=False):
    page = {"confirmed": confirmed, "reason": reason, "text_native": text_native,
            "result": {"template_id": template, "fields": fields} if confirmed else None}
    return {"status": "completed", "attempts": 1, "error": None, "result": {"page_count": 1, "pages": [page]}}


def field(value, confidence, needs_review, tier):
    return {"value": value, "confidence": confidence, "needs_review": needs_review, "approval_tier": tier}


def test_a_confirmed_document_becomes_field_outcomes_with_the_apis_own_verdicts():
    doc = completed({
        "carrier": field("EMIRATES", 96.0, False, "auto"),
        "consignee_name": field("ACME TRADER5", 74.5, False, "spot_check"),  # wrong, but accepted: the dangerous kind
    })

    result = _to_file_result("a.jpg", NEVER_READ, EXPECTED, doc, _validated_fields())

    assert result.classification == OUTCOME_CORRECT and result.error is None and result.extraction_skipped is None
    carrier, consignee = result.fields
    assert carrier.correct and carrier.accepted and carrier.has_validator  # carrier carries a regex in the template
    assert not consignee.correct and consignee.accepted and not consignee.has_validator  # consignee_name has none
    assert result.api_tiers == {"carrier": "auto", "consignee_name": "spot_check"}


def test_a_flagged_field_is_not_accepted():
    doc = completed({"carrier": field("EMIRATE5", 55.0, True, "review"), "consignee_name": field("ACME TRADERS", 90.0, False, "spot_check")})
    assert [f.accepted for f in _to_file_result("a.jpg", NEVER_READ, EXPECTED, doc, {}).fields] == [False, True]


def test_a_document_module_5_did_not_confirm_yields_no_fields():
    result = _to_file_result("a.jpg", NEVER_READ, EXPECTED, completed({}, confirmed=False, reason="no_match"), {})
    assert result.classification == OUTCOME_NEW_TEMPLATE and result.fields == [] and "no_match" in result.extraction_skipped

    ambiguous = _to_file_result("a.jpg", NEVER_READ, EXPECTED, completed({}, confirmed=False, reason="ambiguous_match"), {})
    assert ambiguous.classification == OUTCOME_AMBIGUOUS


def test_a_document_confirmed_as_the_wrong_template_is_called_that():
    doc = completed({"carrier": field("EMIRATES", 96.0, False, "auto")}, template="bits_goa_id_card")
    assert _to_file_result("a.jpg", NEVER_READ, EXPECTED, doc, {}).classification == OUTCOME_WRONG


def test_a_batch_document_that_failed_is_an_error_not_a_zero():
    doc = {"status": "failed", "attempts": 4, "error": "RuntimeError: boom", "result": None}
    result = _to_file_result("a.jpg", NEVER_READ, EXPECTED, doc, {})
    assert "failed after 4 attempt(s): RuntimeError: boom" in result.error and result.fields == []


def test_a_field_the_template_did_not_return_reads_as_empty_and_unaccepted():
    doc = completed({"carrier": field("EMIRATES", 96.0, False, "auto")})  # consignee_name absent
    consignee = _to_file_result("a.jpg", NEVER_READ, EXPECTED, doc, {}).fields[1]
    assert consignee.actual == "" and not consignee.correct and not consignee.accepted


def test_worker_seconds_come_from_the_iso_timestamps():
    assert _seconds("2026-09-24T07:00:00+00:00", "2026-09-24T07:00:04.500000+00:00") == 4.5
    assert _seconds(None, "2026-09-24T07:00:04+00:00") is None


def test_the_validator_map_knows_which_seeded_fields_have_only_confidence():
    validated = _validated_fields()
    assert validated[("air_waybill", "awb_number")] and validated[("air_waybill", "pieces_rcp")]
    assert validated[("air_waybill", "flight_date")]  # since the regex that rejects "EKO565/27FEB"
    assert not validated[("air_waybill", "shipper_name")] and not validated[("air_waybill", "consignee_name")]
