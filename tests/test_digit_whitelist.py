"""
Digit-only fields get one extra, digit-whitelisted Tesseract read, so a "1"
misread as the rule-fragment character "{" (and trimmed to blank) has a real
chance of coming back as "1".
"""

from unittest.mock import Mock

import pytest
from PIL import Image

import app.modules.field_extraction as field_extraction
from app.config import DIGIT_WHITELIST, DIGIT_WHITELIST_PSM
from app.schemas.template import TemplateField

WHITELIST_CONFIG = f"--psm {DIGIT_WHITELIST_PSM} -c tessedit_char_whitelist={DIGIT_WHITELIST}"


def field(regex, data_type="string") -> TemplateField:
    return TemplateField(
        id="f", label="F", bbox=(0.1, 0.1, 0.4, 0.2), data_type=data_type, validation_regex=regex, confidence_floor=0.6
    )


def read(text: str, conf: float) -> dict:
    return {"text": [text], "conf": [conf]} if text else {"text": [], "conf": []}


def ocr_by_config(monkeypatch, plain: dict, whitelisted: dict) -> Mock:
    """image_to_data mock answering `whitelisted` to the digit-whitelist config and `plain` to every other."""
    mock = Mock(side_effect=lambda image, **kw: whitelisted if kw["config"] == WHITELIST_CONFIG else plain)
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", mock)
    return mock


IMAGE = Image.new("RGB", (500, 500), color=255)


@pytest.mark.parametrize(
    "pattern, expected",
    [
        (r"^\d{1,4}$", True),  # pieces_rcp
        ("^[0-9]+$", True),
        (r"^(\d{3}|\d{5})$", True),
        ("^12$", True),
        (r"^\d{3}-?\d{8}$", False),  # AWB number: the hyphen would be deleted
        (r"^[\d.,]{1,10}$", False),  # a weight: the decimal point would be deleted
        (r"^[\d/\-]{6,20}$", False),
        (r"^[A-Z]{3}$", False),
        (r"^\D+$", False),
        ("^[^a-z]+$", False),
        (r"^\d+\s?$", False),
        (r"^\d+.$", False),
        (r"^\d+(?=x)$", False),
        ("^$", False),  # consumes nothing
        ("", False),
        (None, False),
        ("(", False),  # not a regex at all
    ],
)
def test_only_a_regex_that_admits_nothing_but_digits_qualifies(pattern, expected):
    assert field_extraction._accepts_only_digits(pattern) is expected


def test_a_digit_only_field_gets_exactly_one_whitelisted_read_and_it_comes_last(monkeypatch):
    mock = ocr_by_config(monkeypatch, read("", -1), read("", -1))
    field_extraction.extract_field(IMAGE, field(r"^\d{1,4}$"))

    configs = [call.kwargs["config"] for call in mock.call_args_list]
    assert configs.count(WHITELIST_CONFIG) == 1
    assert configs[-1] == WHITELIST_CONFIG
    assert not any("whitelist" in c for c in configs[:-1])


@pytest.mark.parametrize("regex", [r"^[\d.,]{1,10}$", r"^\d{3}-?\d{8}$", None, "^[A-Z]{2}$"])
def test_no_other_field_is_whitelisted(monkeypatch, regex):
    mock = ocr_by_config(monkeypatch, read("ABC", 80), read("999", 99))
    result = field_extraction.extract_field(IMAGE, field(regex))

    assert not any("whitelist" in call.kwargs["config"] for call in mock.call_args_list)
    assert result.raw_text == "ABC"


def test_a_one_read_as_a_brace_is_recovered(monkeypatch):
    """The measured failure: every plain mode reads "{", the trim empties it, the field is blank."""
    ocr_by_config(monkeypatch, read("{", 74), read("1", 31))
    result = field_extraction.extract_field(IMAGE, field(r"^\d{1,4}$"))

    assert result.raw_text == "1"
    assert result.aggregate_confidence == 31  # honest: still under the field's 0.6 floor, so Module 7 still flags it
    assert result.psm_used == DIGIT_WHITELIST_PSM


def test_a_plain_read_that_is_already_right_is_kept_on_a_tie(monkeypatch):
    ocr_by_config(monkeypatch, read("1", 67), read("4", 67))
    assert field_extraction.extract_field(IMAGE, field(r"^\d{1,4}$")).raw_text == "1"


def test_a_more_confident_whitelisted_read_wins(monkeypatch):
    ocr_by_config(monkeypatch, read("7", 50), read("1", 90))
    assert field_extraction.extract_field(IMAGE, field(r"^\d{1,4}$")).raw_text == "1"


def test_an_empty_whitelisted_read_does_not_erase_a_plain_read(monkeypatch):
    """dim_blur on the synthetic corpus: the whitelisted read finds no digit, the plain one reads "iq"."""
    ocr_by_config(monkeypatch, read("iq", 54), read("", -1))
    assert field_extraction.extract_field(IMAGE, field(r"^\d{1,4}$")).raw_text == "iq"
