"""Tiers for a page read by page engines (app/modules/page_tiers.py): auto = grounded AND rules AND agreement."""

import pytest

from app.modules.grounding import PageText
from app.modules.page_tiers import tier_fields
from scripts.seed_templates import AIR_WAYBILL

GOOD_AWB = "176-06067736"  # a valid IATA mod-7 number
PAGE = PageText(layer="", ocr=f"air waybill {GOOD_AWB} emirates dubai")


def _tiers(value=GOOD_AWB, others=None, page=PAGE, fid="awb_number", extra=None):
    primary = {fid: (value, "src")}
    primary.update(extra or {})
    return tier_fields(AIR_WAYBILL, primary, "p1", others if others is not None else {}, page)


def test_grounded_valid_and_agreed_is_auto():
    t = _tiers(others={"p2": {"awb_number": GOOD_AWB}})["awb_number"]
    assert t.tier == "auto" and t.grounded and t.agreed is True and t.engines == ["p1", "p2"] and t.source_text == "src"


def test_a_second_reader_may_be_a_crop_engine():
    t = _tiers(others={"template_text_layer": {"awb_number": GOOD_AWB.lower()}})["awb_number"]
    assert t.tier == "auto"


def test_agreement_uses_the_scorers_normalisation_not_exact_text():
    t = _tiers(others={"p2": {"awb_number": f"  {GOOD_AWB}  "}})["awb_number"]
    assert t.agreed is True


def test_no_second_reader_means_nothing_is_auto_and_it_says_why():
    t = _tiers(others={})["awb_number"]
    assert t.tier == "spot_check" and t.agreed is None and "single_reader" in t.reasons
    assert all(x.tier != "auto" for x in _tiers(others={}).values())


def test_not_on_the_page_is_a_spot_check_even_when_two_readers_agree():
    t = _tiers(others={"p2": {"awb_number": GOOD_AWB}}, page=PageText(layer="", ocr="nothing relevant here"))["awb_number"]
    assert t.tier == "spot_check" and not t.grounded and "not_grounded" in t.reasons


def test_disagreeing_readers_send_the_field_to_review():
    t = _tiers(others={"p2": {"awb_number": "176-06067744"}})["awb_number"]
    assert t.tier == "review" and t.agreed is False and "readers_disagree" in t.reasons


def test_one_agreeing_reader_outweighs_one_that_read_something_else():
    t = _tiers(others={"p2": {"awb_number": "999"}, "crop": {"awb_number": GOOD_AWB}})["awb_number"]
    assert t.tier == "auto" and t.engines == ["p1", "crop"]


def test_a_reader_that_read_nothing_is_no_evidence_either_way():
    t = _tiers(others={"p2": {"awb_number": ""}})["awb_number"]
    assert t.tier == "spot_check" and t.agreed is None


@pytest.mark.parametrize("bad", ["176-06067726", "not an awb"])
def test_a_failed_rule_is_review_however_grounded_and_agreed(bad):
    page = PageText(layer="", ocr=f"awb {bad}")
    t = _tiers(value=bad, others={"p2": {"awb_number": bad}}, page=page)["awb_number"]
    assert t.tier == "review" and t.reasons  # check digit or format


def test_a_blank_is_never_auto():
    t = _tiers(fid="shipper_name", value="", others={"p2": {"shipper_name": ""}})["shipper_name"]
    assert t.tier == "spot_check" and "blank" in t.reasons and not t.grounded


def test_a_digital_page_grounds_only_on_its_text_layer():
    page = PageText(layer=f"air waybill {GOOD_AWB}", ocr="")
    assert _tiers(others={"p2": {"awb_number": GOOD_AWB}}, page=page)["awb_number"].tier == "auto"
    assert _tiers(value="176-06067744", others={"p2": {"awb_number": "176-06067744"}}, page=page)["awb_number"].tier != "auto"


def test_every_template_field_gets_a_result():
    assert set(_tiers()) == {f.id for f in AIR_WAYBILL.fields}
