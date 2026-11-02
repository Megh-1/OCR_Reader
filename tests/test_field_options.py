"""from_field / extract_pattern / squash_spaces / optional: reading one cell once and selecting values from its text."""

import pytest
from pydantic import ValidationError

from app.models.text_layer import TextLayer, TextWord
from app.modules import field_extraction
from app.modules.field_extraction import (
    FieldExtractionResult,
    _apply_field_options,
    extract_fields_from_text_layer,
)
from app.modules.validation import validate_field
from app.schemas.template import TemplateField, TemplateIn


def field(id, **kw):
    base = dict(id=id, label=id, bbox=(0.1, 0.1, 0.5, 0.2), data_type="string", confidence_floor=0.6)
    base.update(kw)
    return TemplateField(**base)


def read(id, text, conf=90.0, source="ocr"):
    return FieldExtractionResult(field_id=id, raw_text=text, word_confidences=[conf] if text else [],
                                 aggregate_confidence=conf, psm_used=6, source=source)


HS = r"H\.?\s?S\.?[\s-]*CODE\s*[:.]?\s*([0-9][0-9 .]{3,12}[0-9])"
CELL = "GARMENTS,HANGERS, ACCESSORIES, AND SCARF H.S.CODE : 61099020 DIMS(CMS) 106x68x43/1"


# --- schema -----------------------------------------------------------------


def test_a_pattern_needs_exactly_one_capture_group():
    with pytest.raises(ValidationError, match="exactly one capture group"):
        field("hs", extract_pattern=r"(\d+)-(\d+)")
    with pytest.raises(ValidationError, match="exactly one capture group"):
        field("hs", extract_pattern=r"\d+")
    with pytest.raises(ValidationError, match="not a valid regex"):
        field("hs", extract_pattern=r"(\d+")


def test_a_derived_field_needs_a_pattern_and_a_different_source():
    with pytest.raises(ValidationError, match="needs an extract_pattern"):
        field("hs", from_field="goods")
    with pytest.raises(ValidationError, match="different field"):
        field("hs", from_field="hs", extract_pattern=r"(\d+)")


def test_optional_is_only_for_a_derived_field():
    with pytest.raises(ValidationError, match="only for a derived field"):
        field("hs", optional=True)


def template_with(*fields):
    return TemplateIn(template_id="t", anchors=[{"text": "A", "bbox": [0.1, 0.1, 0.2, 0.2], "match_threshold": 0.7}], fields=list(fields))


def test_a_template_refuses_a_source_it_does_not_have_and_a_chain_of_derivations():
    goods = field("goods")
    with pytest.raises(ValidationError, match="does not have"):
        template_with(goods, field("hs", from_field="nope", extract_pattern=r"(\d+)"))
    with pytest.raises(ValidationError, match="itself derived"):
        template_with(goods, field("a", from_field="goods", extract_pattern=r"(\d+)"), field("b", from_field="a", extract_pattern=r"(\d+)"))
    template_with(goods, field("hs", from_field="goods", extract_pattern=r"(\d+)"))  # valid


# --- Module 6 ---------------------------------------------------------------


def test_one_cell_read_once_yields_the_description_the_code_and_nothing_for_an_absent_value():
    fields = [
        field("goods"),
        field("nature", from_field="goods", extract_pattern=r"^(.*?)(?:\s+H\.?S\.?[\s-]*CODE|\s+DIMS?\b|$)"),
        field("hs", from_field="goods", extract_pattern=HS),
        field("inv", from_field="goods", extract_pattern=r"INV(?:OICE)?\.?\s*(?:NO\.?)?\s*[:.]?\s*([A-Z0-9][A-Z0-9/-]{3,25})", optional=True),
    ]

    out = _apply_field_options(fields, {"goods": read("goods", CELL, 88.0)})

    assert out["nature"].raw_text == "GARMENTS,HANGERS, ACCESSORIES, AND SCARF"
    assert out["hs"].raw_text == "61099020"
    assert out["inv"].raw_text == "" and out["inv"].aggregate_confidence == 88.0  # absent, and the cell was read
    assert out["hs"].aggregate_confidence == 88.0  # same pixels, same confidence


def test_a_pattern_that_finds_nothing_fails_unless_the_field_is_optional():
    fields = [field("goods"), field("hs", from_field="goods", extract_pattern=HS)]

    out = _apply_field_options(fields, {"goods": read("goods", "PERISHABLE CARGO FRESH VEGETABLES")})

    assert out["hs"].raw_text == "" and out["hs"].aggregate_confidence == 0.0


def test_a_source_that_read_nothing_is_never_evidence_that_a_value_is_absent():
    fields = [field("goods"), field("hs", from_field="goods", extract_pattern=HS, optional=True)]

    out = _apply_field_options(fields, {"goods": read("goods", "", 0.0)})

    assert out["hs"].aggregate_confidence == 0.0


def test_squash_spaces_and_a_pattern_on_a_field_that_reads_its_own_box():
    fields = [
        field("awb", squash_spaces=True),
        field("weight", extract_pattern=r"(\d[\d,.]*)"),
    ]

    out = _apply_field_options(fields, {"awb": read("awb", "176 - 0669  1322"), "weight": read("weight", "AS 500.00")})

    assert out["awb"].raw_text == "176-06691322"
    assert out["weight"].raw_text == "500.00"


def test_fields_without_options_pass_through_untouched():
    f = field("plain")
    r = read("plain", " as read ")

    assert _apply_field_options([f], {"plain": r})["plain"] is r


def test_a_derived_field_is_not_read_from_its_own_box(monkeypatch):
    """Only the source is OCR'd; the derived field's bbox never reaches Tesseract."""
    seen = []
    monkeypatch.setattr(field_extraction, "extract_field", lambda image, f: seen.append(f.id) or read(f.id, CELL))
    fields = [field("goods"), field("hs", from_field="goods", extract_pattern=HS)]
    template = type("T", (), {"fields": fields, "template_id": "t", "version": 1})()
    monkeypatch.setattr(field_extraction, "get_template_version", lambda *a: template)
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("L", (50, 50), 255).save(buf, "PNG")

    out = field_extraction.extract_fields("t", 1, buf.getvalue())

    assert seen == ["goods"]
    assert out["hs"].raw_text == "61099020"


def test_the_text_layer_path_applies_the_same_options(monkeypatch):
    fields = [field("goods", bbox=(0.1, 0.1, 0.9, 0.2)), field("hs", from_field="goods", extract_pattern=HS)]
    template = type("T", (), {"fields": fields, "template_id": "t", "version": 1})()
    monkeypatch.setattr(field_extraction, "get_template_version", lambda *a: template)
    words = tuple(TextWord(t, (0.12 + i * 0.05, 0.12, 0.16 + i * 0.05, 0.14)) for i, t in enumerate(["JACKETS", "HS", "CODE:", "6109", "9020"]))

    out = extract_fields_from_text_layer("t", 1, TextLayer(words=words))

    assert out["goods"].raw_text == "JACKETS HS CODE: 6109 9020"
    assert out["hs"].raw_text == "6109 9020"
    assert out["hs"].source == "text_layer"


# --- Module 7 ---------------------------------------------------------------


def optional_hs():
    return field("hs", from_field="goods", extract_pattern=HS, optional=True, validation_regex=r"\d{4}[\d. ]{0,8}")


def test_an_absent_optional_value_is_accepted_but_never_auto_even_from_exact_pdf_text():
    """Exact text proves the pattern did not match, not that the value is absent ('HS CODE NO. 6109...' missed it)."""
    out = validate_field(optional_hs(), read("hs", "", 100.0, source="text_layer"))

    assert out.accepted and out.approval_tier == "spot_check"


def test_an_absent_optional_value_on_a_scan_is_a_spot_check_never_auto():
    out = validate_field(optional_hs(), read("hs", "", 96.0, source="ocr"))

    assert out.accepted and out.approval_tier == "spot_check"


def test_an_absent_optional_value_from_an_unread_source_is_rejected():
    out = validate_field(optional_hs(), read("hs", "", 0.0, source="ocr"))

    assert not out.accepted and out.reasons == ["low_confidence"]


def test_a_present_optional_value_is_still_format_checked():
    out = validate_field(optional_hs(), read("hs", "6l09 9O20", 95.0))

    assert not out.accepted and "format_invalid" in out.reasons


# --- confidence of the selected words, and empty selections -------------------------------------------


def read_words(id, text, confs, source="ocr"):
    return FieldExtractionResult(field_id=id, raw_text=text, word_confidences=list(confs),
                                 aggregate_confidence=sum(confs) / len(confs), psm_used=6, source=source)


def test_a_derived_value_has_the_confidence_of_its_own_words_not_the_cells_average():
    cell = "GARMENTS AND ACCESSORIES OF COTTON KNITTED HS CODE 6109 9020"
    confs = [96, 96, 95, 97, 96, 95, 96, 94, 30, 25]  # one per word: ordinary words high, the code's two digit groups low
    fields = [field("goods"), field("hs", from_field="goods", extract_pattern=HS)]

    out = _apply_field_options(fields, {"goods": read_words("goods", cell, confs)})

    assert out["hs"].raw_text == "6109 9020"
    assert out["hs"].aggregate_confidence == pytest.approx(27.5)  # the cell as a whole averages 82.0
    assert out["hs"].word_confidences == [30, 25]


def test_when_words_and_confidences_do_not_line_up_the_lowest_confidence_is_used():
    fields = [field("goods"), field("hs", from_field="goods", extract_pattern=HS)]

    out = _apply_field_options(fields, {"goods": read_words("goods", "JACKETS HS CODE 6109 9020", [96, 90, 40])})

    assert out["hs"].aggregate_confidence == 40


def test_an_empty_selection_is_flagged_not_accepted_at_the_cells_confidence():
    """DESCRIPTION matches an empty group when the cell opens with a marker; that is no value, not a confident one."""
    fields = [field("goods"), field("nat", from_field="goods", extract_pattern=r"^\s*(.*?)\s*(?:HS CODE|$)")]

    out = _apply_field_options(fields, {"goods": read("goods", "HS CODE 61099020", 95.0)})

    assert out["nat"].raw_text == "" and out["nat"].aggregate_confidence == 0.0
