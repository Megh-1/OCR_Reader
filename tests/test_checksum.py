"""
Module 7's checksum validation: a check-digit algorithm a template can name on a
field, reported under its OWN reason ("check_digit_invalid") because it catches a
different class of error than a regex does - a value that is shaped right and
wrong, rather than malformed.
"""

import random

import pytest

from app.modules.field_extraction import FieldExtractionResult
from app.modules.template_store import get_latest_template
from app.modules.validation import _iata_awb_mod7, validate_field
from app.schemas.template import TemplateField
from scripts.seed_templates import AIR_WAYBILL, seed

AWB_REGEX = r"^\d{3}-?\s?\d{8}$"  # the air_waybill template's own pattern

# The real values from the synthetic wide-framing camera photo: what the document
# says, and what Tesseract read (one digit off, at confidence 88).
REAL_NUMBER = "176-06067736"
REAL_MISREAD = "176-06067726"


def awb_field(checksum="iata_awb_mod7", regex=AWB_REGEX) -> TemplateField:
    return TemplateField(
        id="awb_number", label="AWB Number", bbox=(0.73, 0.016, 0.875, 0.037),
        data_type="string", validation_regex=regex, confidence_floor=0.60, checksum=checksum,
    )


def read(text: str, confidence: float = 88.0) -> FieldExtractionResult:
    return FieldExtractionResult(
        field_id="awb_number", raw_text=text, word_confidences=[confidence],
        aggregate_confidence=confidence, psm_used=7,
    )


def with_check_digit(serial: int, prefix: str = "176") -> str:
    return f"{prefix}-{serial:07d}{serial % 7}"


# --- the algorithm -------------------------------------------------------------


def test_the_real_waybill_number_passes_and_the_real_misread_fails():
    assert _iata_awb_mod7(REAL_NUMBER) is True  # 606773 mod 7 = 6, check digit 6
    assert _iata_awb_mod7(REAL_MISREAD) is False  # 606772 mod 7 = 5, check digit 6


@pytest.mark.parametrize("formatting", ["{p}-{s}{c}", "{p}{s}{c}", "{p} {s}{c}", "{p}-{s}{c} "])
def test_separators_do_not_change_the_verdict(formatting):
    text = formatting.format(p="176", s="0606773", c="6")
    assert _iata_awb_mod7(text.strip()) is True


def test_every_valid_serial_passes_and_every_other_check_digit_fails():
    for serial in range(0, 20000, 7):
        good = serial % 7
        assert _iata_awb_mod7(with_check_digit(serial)) is True
        for wrong in range(10):
            if wrong != good:
                assert _iata_awb_mod7(f"176-{serial:07d}{wrong}") is False


def test_single_digit_misreads_in_the_serial_are_caught_except_zero_seven_swaps():
    """The property that makes the check worth having, and its one honest hole.
    Changing a serial digit shifts the serial by (difference) x 10^k, and no power
    of ten is divisible by 7 - so any misread is caught UNLESS the two digits differ
    by 7 itself. 0<->7 (and 1<->8, 2<->9) is exactly that."""
    rng = random.Random(7)
    caught = missed = 0
    for _ in range(300):
        serial = rng.randrange(10_000_000)
        digits = f"{serial:07d}"
        position = rng.randrange(7)
        wrong_digit = rng.choice([d for d in "0123456789" if d != digits[position]])
        misread = digits[:position] + wrong_digit + digits[position + 1:]
        verdict = _iata_awb_mod7(f"176-{misread}{serial % 7}")
        differs_by_seven = abs(int(wrong_digit) - int(digits[position])) == 7
        assert verdict is (True if differs_by_seven else False)
        caught += verdict is False
        missed += verdict is True
    assert caught > 0 and missed > 0  # both branches were actually exercised


def test_a_value_the_check_cannot_parse_is_not_judged():
    for unparseable in ("", "abc", "176-0606773", "176-060677366", "AAA-06067736"):
        assert _iata_awb_mod7(unparseable) is None


# --- Module 7 integration ------------------------------------------------------


def test_a_shaped_right_but_wrong_number_is_flagged_for_the_check_and_nothing_else():
    """The case a regex cannot see: right shape, confidence well above the floor,
    one digit wrong. It must be flagged, under a reason that says WHY."""
    verdict = validate_field(awb_field(), read(REAL_MISREAD, confidence=88.0))

    assert verdict.accepted is False
    assert verdict.reasons == ["check_digit_invalid"]  # not format_invalid, not low_confidence
    assert verdict.value == REAL_MISREAD  # kept for the reviewer, never corrected


def test_the_correct_number_is_accepted():
    verdict = validate_field(awb_field(), read(REAL_NUMBER))
    assert verdict.accepted is True and verdict.reasons == []


def test_without_a_checksum_the_same_wrong_number_still_ships():
    """Documents what the field did before this existed - the false accept."""
    verdict = validate_field(awb_field(checksum=None), read(REAL_MISREAD, confidence=88.0))
    assert verdict.accepted is True


def test_a_malformed_value_is_the_regexs_finding_not_the_checks():
    verdict = validate_field(awb_field(), read("176-0606"))
    assert verdict.reasons == ["format_invalid"]  # no check_digit_invalid double-report


def test_every_failed_check_is_reported_together():
    verdict = validate_field(awb_field(), read(REAL_MISREAD, confidence=20.0))
    assert verdict.reasons == ["check_digit_invalid", "low_confidence"]


# --- the template schema ---------------------------------------------------------


def test_the_seeded_air_waybill_carries_the_check_on_awb_number_only():
    seed([AIR_WAYBILL])
    template = get_latest_template("air_waybill")

    with_checksum = [f.id for f in template.fields if f.checksum]
    assert with_checksum == ["awb_number"]
    assert next(f for f in template.fields if f.id == "awb_number").checksum == "iata_awb_mod7"


def test_an_unknown_checksum_name_is_rejected_when_the_template_is_saved(client, unique_doc_type):
    payload = {
        "template_id": unique_doc_type,
        "anchors": [{"text": "X", "bbox": [0.0, 0.0, 0.1, 0.1], "match_threshold": 0.85}],
        "fields": [{
            "id": "f", "label": "F", "bbox": [0.1, 0.1, 0.4, 0.2], "data_type": "string",
            "validation_regex": None, "confidence_floor": 0.75, "checksum": "luhn",
        }],
    }
    assert client.post("/templates", json=payload).status_code == 422  # a typo must not silently check nothing

    payload["fields"][0]["checksum"] = "iata_awb_mod7"
    saved = client.post("/templates", json=payload)
    assert saved.status_code == 201
    assert saved.json()["fields"][0]["checksum"] == "iata_awb_mod7"


def test_templates_saved_before_checksums_existed_still_load(client, unique_doc_type):
    payload = {
        "template_id": unique_doc_type,
        "anchors": [{"text": "X", "bbox": [0.0, 0.0, 0.1, 0.1], "match_threshold": 0.85}],
        "fields": [{
            "id": "f", "label": "F", "bbox": [0.1, 0.1, 0.4, 0.2], "data_type": "string",
            "validation_regex": None, "confidence_floor": 0.75,  # no checksum key at all
        }],
    }
    assert client.post("/templates", json=payload).json()["fields"][0]["checksum"] is None
