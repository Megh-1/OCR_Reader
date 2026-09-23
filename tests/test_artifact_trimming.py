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


# --- opening quotes: Tesseract reads a cell's LEFT BORDER as one ----------------
# Measured on the synthetic corpus: a curly or straight opening quote leads a raw
# read in 11 (U+201C), 8 (") and 6 (U+2018) of 19 files. The bytes Tesseract emits
# are valid UTF-8 (e2 80 9c / e2 80 98), so this is the model, not a decoding fault.
# Written with escapes, not literals, so the file's encoding can't matter.

LDQ, RDQ, LSQ, RSQ, EM = "“", "”", "‘", "’", "—"


@pytest.mark.parametrize(
    "read, expected",
    [
        (f"{LDQ}VTS CLIMA LLC", "VTS CLIMA LLC"),  # the real consignee read
        (f"{LSQ}VTS TF AIR SYSTEMS PVT LTD}}", "VTS TF AIR SYSTEMS PVT LTD"),  # real shipper read, right border too
        ('"14/03/0578', "14/03/0578"),  # a straight quote, real agent code read
        (f"{EM}{LDQ} VTS CLIMA LLC", "VTS CLIMA LLC"),  # two border marks in a row
        (f"{LDQ}{LSQ}VTS CLIMA LLC", "VTS CLIMA LLC"),
    ],
)
def test_an_unmatched_opening_quote_from_a_cell_border_is_trimmed(read, expected):
    assert _trim_rule_artifacts(read) == expected


@pytest.mark.parametrize(
    "value",
    [
        '"ACME" TRADERS',  # a matched pair is content, not a border
        f"{LDQ}ACME{RDQ} TRADERS",
        f"{LSQ}ACME{RSQ} TRADERS",
        "O'BRIEN & SONS",  # an apostrophe inside a word is never at the edge
        f"SHIPPER{RSQ}S NAME",
    ],
)
def test_quotes_that_belong_to_the_value_are_left_alone(value):
    assert _trim_rule_artifacts(value) == value


def test_the_em_dash_is_a_border_artifact_at_either_edge():
    assert _trim_rule_artifacts(f"{EM} VTS CLIMA LLC") == "VTS CLIMA LLC"
    assert _trim_rule_artifacts(f"13752.00 {EM}") == "13752.00"


def test_no_verified_field_value_is_changed_by_trimming():
    """The safety net. Every ground-truth value for the air waybill and the ID
    card must pass through the trim untouched - if a legitimate value ever loses a
    character here, it would be silently corrupted before validation sees it."""
    truth = [
        "176-06067736", "EMIRATES", "VTS TF AIR SYSTEMS PVT LTD", "VTS CLIMA LLC", "TEAMGLOBAL LOGISTICS PVT LTD",
        "14/03/0578", "BANGALORE (BLR)", "DUBAI", "DXB", "EK", "EK0565/27FEB", "INR", "PP", "NVD", "NVC",
        "FREIGHT PREPAID", "1", "65", "Q", "170", "11050", "PARTS OF AIR CONDITIONING M/C.", "500.00", "2202.00",
        "13752.00", "26-Feb-2025", "BANGALORE", "Megh Brijesh Shah", "2024A7PS0476G", "17-Oct-2006",
        "HS CODE: 84159000", "INV: EX/KA/0057/24-25",
    ]
    for value in truth:
        assert _trim_rule_artifacts(value) == value, value
