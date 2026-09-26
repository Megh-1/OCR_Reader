"""
Module 7 approval tiers: review (rejected), spot_check (accepted, but under the auto
threshold or with no regex/checksum) and auto (accepted, validated, at or above the
threshold), carried through to the output JSON.
"""

from unittest.mock import Mock

import pytest

import app.modules.field_extraction as field_extraction
import app.modules.type_identification as type_identification
import app.modules.validation as validation
from app.modules.output_compiler import compile_output
from app.modules.validation import approval_tier, has_validator, validate_field, validate_fields
from app.schemas.extraction_result import CompiledResult, FieldResult
from tests.test_field_extraction import ocr_responses
from tests.test_pipeline import make_image_bytes, save_template
from tests.test_validation import make_extraction, make_field


@pytest.fixture()
def threshold_80(monkeypatch):
    monkeypatch.setattr(validation, "CONFIDENCE_AUTO_APPROVE_THRESHOLD", 0.80)


def test_a_rejected_field_is_review_however_confident(threshold_80):
    assert approval_tier(accepted=False, aggregate_confidence=100.0, has_validator=True) == "review"
    assert approval_tier(accepted=False, aggregate_confidence=100.0, has_validator=False) == "review"


def test_an_accepted_field_under_the_threshold_is_a_spot_check(threshold_80):
    assert approval_tier(accepted=True, aggregate_confidence=79.9, has_validator=True) == "spot_check"


def test_a_field_exactly_on_the_threshold_is_auto(threshold_80):
    """Same edge as the per-field floor: exactly on the line passes."""
    assert approval_tier(accepted=True, aggregate_confidence=80.0, has_validator=True) == "auto"


def test_a_text_native_field_is_auto_when_it_validates(threshold_80):
    assert approval_tier(accepted=True, aggregate_confidence=100.0, has_validator=True) == "auto"


def test_a_field_with_no_validator_is_never_auto_however_confident(threshold_80):
    """Confidence alone never auto-approves: every wrong value Module 7 accepted on the
    synthetic corpus was in a field with nothing else to judge it. Not even the fixed 100
    of a text-native read, which says the PDF held those words, not that they are right."""
    for confidence in (80.0, 99.9, 100.0):
        assert approval_tier(accepted=True, aggregate_confidence=confidence, has_validator=False) == "spot_check"


def test_a_missing_validator_never_makes_an_accepted_field_review(threshold_80):
    """The rule only withholds auto; it does not reject what Module 7 accepted."""
    assert approval_tier(accepted=True, aggregate_confidence=10.0, has_validator=False) == "spot_check"


def test_a_regex_or_a_checksum_each_counts_as_a_validator():
    assert has_validator(make_field(validation_regex=r"^\d+$"))
    assert has_validator(make_field().model_copy(update={"checksum": "iata_awb_mod7"}))
    assert not has_validator(make_field())


def test_validate_field_assigns_the_tier(threshold_80):
    field = make_field(validation_regex=r"^[A-Za-z ]+$", confidence_floor=0.5)

    assert validate_field(field, make_extraction(raw_text="John", aggregate_confidence=95.0)).approval_tier == "auto"
    assert validate_field(field, make_extraction(raw_text="John", aggregate_confidence=65.0)).approval_tier == "spot_check"


def test_validate_field_holds_a_confident_unvalidated_field_at_spot_check(threshold_80):
    field = make_field(validation_regex=None, confidence_floor=0.5)
    result = validate_field(field, make_extraction(raw_text="ACME TRADERS", aggregate_confidence=99.0))

    assert result.accepted and result.approval_tier == "spot_check"


def test_each_way_of_being_rejected_is_review(threshold_80):
    field = make_field(validation_regex=r"^[A-Za-z ]+$", confidence_floor=0.5)

    wrong_format = validate_field(field, make_extraction(raw_text="J0hn", aggregate_confidence=99.0))
    under_floor = validate_field(field, make_extraction(raw_text="John", aggregate_confidence=40.0))

    assert wrong_format.approval_tier == under_floor.approval_tier == "review"
    assert wrong_format.reasons == ["format_invalid"] and under_floor.reasons == ["low_confidence"]


def test_a_spot_check_is_still_accepted_and_does_not_flag_the_record(threshold_80):
    """The tier adds caution on top of validation; it never turns an accepted field into a rejected one."""
    template = Mock(fields=[make_field(field_id="a", confidence_floor=0.5)])
    summary = validate_fields(template, {"a": make_extraction("a", "text", 65.0)})

    assert "a" in summary.accepted and not summary.flagged
    assert compile_output("t", 1, summary).overall_status == "complete"


def test_the_output_carries_each_fields_tier(threshold_80):
    template = Mock(fields=[
        make_field(field_id="sure", confidence_floor=0.5, validation_regex=r"^[a-z]+$"),
        make_field(field_id="unsure", confidence_floor=0.5, validation_regex=r"^[a-z]+$"),
        make_field(field_id="unchecked", confidence_floor=0.5),
        make_field(field_id="bad", confidence_floor=0.5, validation_regex=r"^\d+$"),
    ])
    summary = validate_fields(template, {
        "sure": make_extraction("sure", "abc", 95.0),
        "unsure": make_extraction("unsure", "abc", 65.0),
        "unchecked": make_extraction("unchecked", "abc", 95.0),
        "bad": make_extraction("bad", "abc", 99.0),
    })

    fields = compile_output("t", 1, summary).fields

    assert {k: v.approval_tier for k, v in fields.items()} == {
        "sure": "auto", "unsure": "spot_check", "unchecked": "spot_check", "bad": "review",
    }
    assert [k for k, v in fields.items() if v.needs_review] == ["bad"]


def test_a_result_stored_before_tiers_existed_still_loads():
    legacy = {
        "template_id": "t", "version": 1, "extracted_at": "2026-09-01T00:00:00Z", "overall_status": "complete",
        "fields": {"a": {"field_id": "a", "value": "x", "confidence": 90.0, "needs_review": False, "reasons": []}},
    }
    assert CompiledResult(**legacy).fields["a"].approval_tier is None


def test_an_unknown_tier_is_rejected_by_the_schema():
    with pytest.raises(ValueError):
        FieldResult(field_id="a", value="x", confidence=1.0, needs_review=False, reasons=[], approval_tier="probably_fine")


def test_the_api_response_reports_the_tier(client, unique_doc_type, monkeypatch):
    """One field per request, so the mocked reads cannot interleave across the extraction thread pool."""
    monkeypatch.setattr(validation, "CONFIDENCE_AUTO_APPROVE_THRESHOLD", 0.80)
    save_template(client, unique_doc_type, [
        {"id": "name", "label": "N", "bbox": [0.1, 0.2, 0.5, 0.3], "data_type": "string",
         "validation_regex": r"^[A-Za-z ]+$", "confidence_floor": 0.5},
    ])
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))

    def process(confidence):
        monkeypatch.setattr(
            field_extraction.pytesseract, "image_to_data",
            Mock(side_effect=ocr_responses(("string", {"text": ["Some", "Name"], "conf": [confidence, confidence]}))),
        )
        resp = client.post(
            "/documents/process",
            files={"file": ("sample.jpg", make_image_bytes(), "image/jpeg")},
            data={"type_hint": unique_doc_type},
        )
        assert resp.status_code == 200
        return resp.json()["pages"][0]

    clear, hazy = process(96), process(66)

    assert clear["result"]["fields"]["name"]["approval_tier"] == "auto"
    assert hazy["result"]["fields"]["name"]["approval_tier"] == "spot_check"
    assert hazy["result"]["overall_status"] == "complete"  # a spot check does not hold the record back
    stored = client.get(f"/results/{hazy['extraction_result_id']}").json()
    assert stored["fields"]["name"]["approval_tier"] == "spot_check"  # persisted, not just returned
