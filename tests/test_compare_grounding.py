"""The comparison rules and grounding the sprint's scorer and tiers share (app/utils/compare.py, app/modules/grounding.py)."""

import pytest

from app.models.text_layer import TextLayer, TextWord
from app.modules.grounding import PageText, page_text
from app.utils.compare import values_agree


def test_whitespace_and_case_are_forgiven_nothing_else():
    assert values_agree("shipper_name", "  Acme   Ltd ", "ACME LTD")
    assert not values_agree("shipper_name", "ACME LTD.", "ACME LTD")


def test_numeric_fields_compare_as_numbers_without_thousands_commas():
    assert values_agree("total_charge", "1,234.00", "1234")
    assert values_agree("gross_weight", "83.320", "83.32")
    assert not values_agree("total_charge", "1,234.50", "1234")
    assert not values_agree("shipper_name", "1,234", "1234")  # only numeric fields get this


def test_leading_printed_label_is_dropped_for_hs_code_and_invoice():
    assert values_agree("hs_code", "HS CODE NO. 6109.10", "6109.10")
    assert values_agree("invoice_reference", "INV: 4521", "4521")
    assert not values_agree("nature_of_goods", "HS CODE NO. 6109.10", "6109.10")


def test_a_blank_equals_a_blank_and_nothing_else():
    assert values_agree("hs_code", "", None)
    assert not values_agree("hs_code", "6109.10", "")


def _layer(*words):
    return TextLayer(words=tuple(TextWord(text=w, bbox=(0.1, 0.1, 0.2, 0.12)) for w in words))


def test_digital_values_must_sit_in_the_text_layer():
    pt = page_text(None, _layer("DUBAI", "176-06067736", "EMIRATES"), ocr=False)
    assert pt.grounded("176-06067736")
    assert not pt.grounded("176-06067737")
    assert not pt.grounded("")


def test_scan_values_are_grounded_by_a_fuzzy_match_with_the_ocr_words():
    pt = PageText(layer="", ocr="shipper acme trading llc consignee globex gmbh")
    assert pt.grounded("ACME TRADING LLC")
    assert pt.grounded("acme tradng llc")  # one dropped letter still matches at >= 90
    assert not pt.grounded("WAYNE ENTERPRISES")


def test_best_score_is_zero_for_a_blank_value():
    assert PageText(layer="x", ocr="y").best_score("  ") == 0.0


def test_the_scorer_refuses_the_test_split():
    from scripts.score_real import score

    with pytest.raises(SystemExit):
        score({"docs": {}}, "test")


def test_digital_grounding_is_whole_printed_words_punctuation_included():
    pt = PageText(layer='tricon enterprises pvt. ltd., "freight prepaid" eap', ocr="")
    assert pt.grounded("TRICON ENTERPRISES PVT. LTD.,")
    assert not pt.grounded("TRICON ENTERPRISES PVT. LTD.")  # the printed comma was dropped
    assert not pt.grounded("FREIGHT PREPAID")  # the printed quotes were dropped
    assert pt.grounded('"FREIGHT PREPAID" EAP')
    assert not pt.grounded("ENTERPRISES PVT")  # 'pvt' is not the printed word 'pvt.'
    assert pt.grounded("ENTERPRISES PVT.")  # a run of whole words from the middle of the line is still on the page
