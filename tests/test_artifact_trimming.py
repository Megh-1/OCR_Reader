"""
_trim_rule_artifacts removes form-rule fragments from the ENDS of an OCR read.
It once treated parentheses as such fragments, which truncated a real value:
"BANGALORE (BLR)" became "BANGALORE (BLR" and - with no regex on that field -
shipped unflagged.
"""

from unittest.mock import Mock

import pytest
from PIL import Image

import app.modules.field_extraction as field_extraction
from app.modules.field_extraction import _trim_rule_artifacts
from app.schemas.template import TemplateField


@pytest.mark.parametrize(
    "value",
    ["BANGALORE (BLR)", "(FOR SHIPPER)", "M/C.", "EK0565/27FEB", "14/03/0578", "PARTS OF AIR CONDITIONING M/C."],
)
def test_legitimate_values_survive_untouched(value):
    assert _trim_rule_artifacts(value) == value


@pytest.mark.parametrize(
    "read, expected",
    [
        ("| 170", "170"),
        ("_ EMIRATES", "EMIRATES"),
        ("~ 500.00", "500.00"),
        ("/ 2202.00", "2202.00"),
        ("BANGALORE _", "BANGALORE"),
        ("EK0565/27FEB |", "EK0565/27FEB"),
        ("[170]", "170"),
        ("  |  ", ""),
    ],
)
def test_rule_fragments_are_still_trimmed_from_the_edges(read, expected):
    assert _trim_rule_artifacts(read) == expected


@pytest.mark.parametrize(
    "read, expected",
    [
        ("170)", "170"),      # a closer with no opener is a stray mark
        ("(170", "170"),      # so is an opener with no closer
        (")170", "170"),      # a closer at the very start can never have a partner
        ("170 (", "170"),     # nor an opener at the very end
        ("BANGALORE (BLR))", "BANGALORE (BLR)"),  # only the surplus one goes
    ],
)
def test_parentheses_are_judged_by_balance_not_by_character(read, expected):
    assert _trim_rule_artifacts(read) == expected


def test_a_value_ending_in_a_paren_survives_a_full_field_read(monkeypatch):
    """The regression, end to end: Tesseract returns the words, and the
    closing paren must still be there in the extracted value."""
    monkeypatch.setattr(
        field_extraction.pytesseract,
        "image_to_data",
        Mock(return_value={"text": ["BANGALORE", "(BLR)"], "conf": [88, 88]}),
    )
    f = TemplateField(
        id="airport_of_departure", label="Airport of Departure", bbox=(0.1, 0.1, 0.5, 0.2),
        data_type="string", validation_regex=None, confidence_floor=0.6,
    )

    result = field_extraction.extract_field(Image.new("RGB", (400, 200), color=255), f)

    assert result.raw_text == "BANGALORE (BLR)"
