"""The Bangkok-form template (scripts/waybill_forms.py) and the patterns that decide what it auto-approves.

Each pattern is tested with strings it must take and strings it must refuse: the refusals are the ones that
were once accepted (an HS code truncated at a misread 0, "INVERTER" read as an invoice marker, a 13th month).
"""

import re

import pytest

from app.models.text_layer import TextLayer, TextWord
from app.modules.field_extraction import FieldExtractionResult, _apply_field_options, extract_fields_from_text_layer
from app.modules.validation import validate_field
from app.schemas.template import TemplateOut
from scripts import waybill_forms as wf
from scripts.seed_templates import ALL_TEMPLATES


def search(pattern, text):
    m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)  # how field_extraction._select applies one
    return m.group(1).strip() if m else None


# --- the template as a whole --------------------------------------------------------------------------------------------


def test_the_template_is_valid_and_seedable():
    t = wf.AIR_WAYBILL_BKK

    assert t.template_id == "air_waybill_bkk"
    assert len(t.fields) == 33 and len(t.anchors) == 2 and len(t.landmarks) > 100
    assert t in ALL_TEMPLATES  # seed_templates.py sees it
    TemplateOut(version=1, created_at="2026-01-01", **t.model_dump(mode="json"))  # round-trips through the stored shape


def test_no_two_fields_share_an_id_and_every_derived_field_has_a_real_source():
    ids = [f.id for f in wf.AIR_WAYBILL_BKK.fields]

    assert len(ids) == len(set(ids))
    assert {f.from_field for f in wf.AIR_WAYBILL_BKK.fields if f.from_field} <= set(ids)


def test_the_two_declared_value_fields_share_one_pattern():
    by_id = {f.id: f for f in wf.AIR_WAYBILL_BKK.fields}

    assert by_id["declared_value_carriage"].validation_regex == by_id["declared_value_customs"].validation_regex == wf.DECLARED_VALUE


def test_seed_templates_uses_the_one_landmark_loader():
    from scripts import seed_templates

    assert seed_templates._landmarks is wf.landmarks


# --- money, weights, identifiers -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["65,000.00", "11050", "83.320", "1,432.00", "0.5", "42444.00"])
def test_numbers_take_a_thousands_comma_or_none_then_a_decimal_point(value):
    assert re.fullmatch(wf.NUMBER, value)


@pytest.mark.parametrize("value", ["642635,35", "1,43,200.00", "1.432,00", "AS AGREED", "MIN", "", "12,34"])
def test_numbers_refuse_a_decimal_comma_and_words(value):
    assert not re.fullmatch(wf.NUMBER, value)


@pytest.mark.parametrize("raw,expected", [("176-0669 1322", "176-0669 1322"), ("205 - 6129 0526", "205 - 6129 0526"), ("junk 176-06067736 x", "176-06067736")])
def test_an_awb_number_is_found_in_a_box_that_holds_other_text(raw, expected):
    assert search(wf.AWB_FROM_BOX, raw) == expected


@pytest.mark.parametrize("raw,expected", [("AS 500.00", "500.00"), ("76.00 K.", "76.00"), ("1,432.00", "1,432.00")])
def test_the_first_number_drops_a_printed_prefix_or_unit(raw, expected):
    assert search(wf.FIRST_NUMBER, raw) == expected


@pytest.mark.parametrize("value", ["EK375/30", "AF195/30JAN", "G9817/30", "NH184 /31", "OV-235/26"])
def test_flight_numbers(value):
    assert re.fullmatch(wf.FLIGHT, value)


@pytest.mark.parametrize("value", ["EK 0507 27/02/2025", "TG", "375/30", ""])
def test_flight_refuses_what_is_not_a_flight_and_date(value):
    assert not re.fullmatch(wf.FLIGHT, value)


@pytest.mark.parametrize("value", ["NVD", "N.V.D.", "N.V.D", "NCV", "N.C.V.", "NVC"])
def test_declared_value_notations(value):
    assert re.fullmatch(wf.DECLARED_VALUE, value)


# --- dates: as strict as the calendar ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["28/01/2025", "26-Feb-2025", "26.02.2025", "January 29, 2025", "JANUARY 29 ,2025", "JAN 29, 2025", "29-JAN-2025", "1/2/2025"])
def test_dates_as_these_forms_print_them(value):
    assert re.fullmatch(wf.DATE, value)


@pytest.mark.parametrize("value", ["99/99/2025", "28/13/2025", "00-00-2025", "32/01/2025", "29-JANX-2025x", "January 32, 2025", "17-Oci-2006"])
def test_dates_refuse_what_the_calendar_does_not_have(value):
    assert not re.fullmatch(wf.DATE, value)


# --- the goods cell: description, HS code, invoice ------------------------------------------------------------------------


GOODS = "GARMENTS,HANGERS, ACCESSORIES, AND SCARF H.S.CODE : 61099020 DIMS(CMS) 106x68x43/1"


def test_the_description_is_what_comes_before_the_first_marker():
    assert search(wf.DESCRIPTION, GOODS) == "GARMENTS,HANGERS, ACCESSORIES, AND SCARF"
    assert search(wf.DESCRIPTION, '"PERSONAL EFFECTS" H.S. CODE 6309.00 H.S. CODE 6405.90') == '"PERSONAL EFFECTS"'
    assert search(wf.DESCRIPTION, "FROZEN CRAB INVOICE NO. TF094/2025 DATE: 29-JANUARY-2025") == "FROZEN CRAB"


@pytest.mark.parametrize("text,code", [
    ("GARMENTS HS CODE:620610", "620610"),
    ("H.S.CODE : 61099020 DIMS", "61099020"),
    ("PARTS HS CODE: 84159000", "84159000"),
    ("HS.CODE: 2106 9099 UN1845", "2106 9099"),
    ("GARMENTS HS CODE NO. 61099020", "61099020"),  # a printed 'NO.' between the marker and the digits
    ("H.S. CODE 6309.00", "6309.00"),
])
def test_an_hs_code_is_taken_when_it_is_printed_in_one_of_these_ways(text, code):
    assert search(wf.HS_CODE, text) == code
    assert re.fullmatch(wf.HS_CODE_VALID, code)


@pytest.mark.parametrize("text", ["HS CODE 6109 9O2O", "HS CODE 6l09 9020", "PARTS OF MACHINES", "HSN 6109 9020"])
def test_an_hs_code_that_does_not_end_cleanly_is_not_taken_in_part(text):
    """A misread character used to cut the capture short ('6109 9'), a well-formed but wrong code."""
    assert search(wf.HS_CODE, text) is None


@pytest.mark.parametrize("text,number", [
    ("INV: EX/KA/0057/24-25 HS CODE 84159000", "EX/KA/0057/24-25"),
    ("INVOICE NO. SAM 250007 DATE: 23-JAN-2025", "SAM 250007"),
    ("Inv.No.:T/PR/364/2024-25 DT: 21.02.2025", "T/PR/364/2024-25"),
    ("INVOICE NO : RT-250127S DATE: JANUARY 27, 2025", "RT-250127S"),
    ("INV NO: AHE/MF/036/24-25 INV DATE: 25.02.2025", "AHE/MF/036/24-25"),
])
def test_an_invoice_number_is_taken_up_to_the_next_printed_marker(text, number):
    assert search(wf.INVOICE, text) == number


@pytest.mark.parametrize("text", ["SOLAR INVERTER 2KW SPARES", "INVENTORY OF PARTS", "INVISIBLE INK", "CARGO WITHOUT ANY REFERENCE"])
def test_a_cargo_word_that_starts_with_inv_is_not_an_invoice_marker(text):
    assert search(wf.INVOICE, text) is None


# --- the three derived fields together, and what Module 7 does with them -------------------------------------------------


def goods_read(text, conf=95.0, source="ocr"):
    words = text.split()
    return FieldExtractionResult("nature_of_goods", text, [conf] * len(words), conf, 6, source=source)


def derived(text, **kw):
    fields = {f.id: f for f in wf.AIR_WAYBILL_BKK.fields}
    sub = [fields["nature_of_goods"], fields["hs_code"], fields["invoice_reference"]]
    return _apply_field_options(sub, {"nature_of_goods": goods_read(text, **kw)}), fields


def test_a_goods_cell_yields_the_description_the_code_and_the_invoice():
    out, fields = derived("PARTS OF AC INV: EX/KA/0057/24-25 HS CODE: 84159000 DIMS 122x85")

    assert out["nature_of_goods"].raw_text == "PARTS OF AC"
    assert out["hs_code"].raw_text == "84159000"
    assert out["invoice_reference"].raw_text == "EX/KA/0057/24-25"
    assert validate_field(fields["hs_code"], out["hs_code"]).accepted


def test_a_cell_with_neither_code_nor_invoice_gives_two_blanks_that_a_person_still_sees():
    out, fields = derived("PERISHABLE CARGO FRESH VEGETABLES", source="text_layer")

    for fid in ("hs_code", "invoice_reference"):
        result = validate_field(fields[fid], out[fid])
        assert out[fid].raw_text == "" and result.accepted and result.approval_tier == "spot_check"  # never auto


def test_a_wrong_invoice_from_a_cargo_word_cannot_reach_the_description_check():
    out, fields = derived("SOLAR INVERTER 2KW SPARES")

    assert out["invoice_reference"].raw_text == ""


def test_the_text_layer_path_reads_a_whole_cell_once_and_derives_from_it(monkeypatch):
    from app.modules import field_extraction

    t = TemplateOut(version=1, created_at="2026-01-01", **wf.AIR_WAYBILL_BKK.model_dump(mode="json"))
    monkeypatch.setattr(field_extraction, "get_template_version", lambda *a: t)
    x0, y0, x1, y1 = t.fields[[f.id for f in t.fields].index("nature_of_goods")].bbox
    words = tuple(TextWord(w, (x0 + 0.01 + i * 0.03, y0 + 0.01, x0 + 0.035 + i * 0.03, y0 + 0.02)) for i, w in enumerate("JACKETS HS CODE: 6109 9020".split()))

    out = extract_fields_from_text_layer("air_waybill_bkk", 1, TextLayer(words=words))

    assert out["nature_of_goods"].raw_text == "JACKETS"
    assert out["hs_code"].raw_text == "6109 9020"
    assert out["hs_code"].aggregate_confidence == 100.0
