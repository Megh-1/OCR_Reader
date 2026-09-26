import re

from app.models.text_layer import TextWord
from app.modules.field_extraction import TEXT_LAYER_MIN_WORD_OVERLAP, _overlap_of_word
from scripts.seed_templates import AIR_WAYBILL


def _field(field_id):
    return next(f for f in AIR_WAYBILL.fields if f.id == field_id)


def test_shipper_name_box_starts_below_the_printed_label_and_above_the_value():
    """On the RASTER path the printed label "Shipper's Name and Address" is real
    ink, unlike in the PDF text layer the box was first derived from. Measured on a
    200 DPI render of the waybill: label ink ends at y=0.0509, the value's ink
    starts at y=0.0545 (an 8 px gap). A top edge inside the label reads its lower
    half into the value ("Shipper's Name and Address VTS TF AIR ..."); one inside
    the value clips it. Pinned to the reason, not to a magic number."""
    top = next(f for f in AIR_WAYBILL.fields if f.id == "shipper_name").bbox[1]
    label_ink_bottom, value_ink_top = 0.0509, 0.0545
    assert label_ink_bottom < top < value_ink_top


def test_flight_date_regex_rejects_the_misread_that_was_accepted():
    """"EKO565/27FEB" (letter O for the zero) was accepted at confidence up to 87, the most
    confident wrong read on the synthetic corpus, when flight_date had no regex. The month is
    enumerated for the same reason the ID card's is."""
    pattern = next(f for f in AIR_WAYBILL.fields if f.id == "flight_date").validation_regex

    assert re.fullmatch(pattern, "EK0565/27FEB")
    assert re.fullmatch(pattern, "EKO565/27FEB") is None
    assert re.fullmatch(pattern, "EK0565/27FE8") is None


def test_only_awb_number_claims_a_checksum():
    """No speculative checksums: a field earns one only where the domain defines a
    real algorithm. Adding another should be a deliberate edit that breaks this."""
    assert [f.id for f in AIR_WAYBILL.fields if f.checksum] == ["awb_number"]


# --- name/address box overlap -------------------------------------------------------
#
# extract_field_from_text_layer (Module 6, text-native path) decides per FIELD whether a
# word belongs to it - nothing stops the same word counting toward two fields whose boxes
# overlap. shipper_name/consignee_name/agent_name each used to extend far enough down to
# swallow the printed line below them, on the pure text-layer path at 100% confidence (no
# OCR, so not a misread - a box geometry bug). Word coordinates below are copied from
# `extract_text_layers` run against tests/fixtures/synthetic_awb/reference__original_digital.pdf
# at 200 DPI page geometry - real measurements, not invented, so these tests don't need the
# untracked fixture file to run.

# "VTS TF AIR SYSTEMS PVT LTD" - shipper_name's own value line.
_SHIPPER_VALUE_WORD = TextWord(text="LTD", bbox=(0.2811, 0.0518, 0.3065, 0.0640))
# "SURVEY NO.13/1, KANEKAL VILLAGE" - shipper_address's first line, previously bled in.
_SHIPPER_ADDRESS_WORD = TextWord(text="SURVEY", bbox=(0.1062, 0.0631, 0.1590, 0.0754))

# "VTS CLIMA LLC" - consignee_name's own value line.
_CONSIGNEE_VALUE_WORD = TextWord(text="LLC", bbox=(0.1831, 0.1427, 0.2068, 0.1550))
# "13-C, PLOT NO. 532-242," - consignee_address's first line, previously bled in.
_CONSIGNEE_ADDRESS_WORD = TextWord(text="13-C,", bbox=(0.1062, 0.1540, 0.1426, 0.1663))

# "TEAMGLOBAL LOGISTICS PVT LTD" - agent_name's own value line.
_AGENT_VALUE_WORD = TextWord(text="LTD", bbox=(0.3328, 0.2336, 0.3614, 0.2474))
# "BANGALORE" - the agent's printed city, one line below, previously bled in (no field of
# its own to overlap; agent_name's box was simply taller than its one line of text).
_AGENT_CITY_WORD = TextWord(text="BANGALORE", bbox=(0.1062, 0.2462, 0.1980, 0.2600))


def test_shipper_name_no_longer_bleeds_into_shipper_address():
    """Regression guard for a real false accept: on the text-native path, at 100% confidence,
    shipper_name used to read 'VTS TF AIR SYSTEMS PVT LTD SURVEY' - the start of the address
    field printed directly below it. Fixed by shrinking shipper_name's bottom edge; verified
    here against the exact function Module 6 uses, not just against the box coordinates."""
    box = tuple(_field("shipper_name").bbox)
    assert _overlap_of_word(_SHIPPER_ADDRESS_WORD, box) < TEXT_LAYER_MIN_WORD_OVERLAP
    assert _overlap_of_word(_SHIPPER_VALUE_WORD, box) >= TEXT_LAYER_MIN_WORD_OVERLAP


def test_consignee_name_no_longer_bleeds_into_consignee_address():
    """Same bug, same fix, as shipper_name above: consignee_name used to read
    'VTS CLIMA LLC 13-C, PLOT NO.' on the text-native path."""
    box = tuple(_field("consignee_name").bbox)
    assert _overlap_of_word(_CONSIGNEE_ADDRESS_WORD, box) < TEXT_LAYER_MIN_WORD_OVERLAP
    assert _overlap_of_word(_CONSIGNEE_VALUE_WORD, box) >= TEXT_LAYER_MIN_WORD_OVERLAP


def test_agent_name_no_longer_bleeds_into_the_printed_city_line_below_it():
    """Same failure mode, different cause: agent_name has no neighbouring field box to
    overlap - it was simply taller than the one line of text it holds, and swallowed the
    printed city ('BANGALORE') below it. Used to read 'TEAMGLOBAL LOGISTICS PVT LTD BANGA'."""
    box = tuple(_field("agent_name").bbox)
    assert _overlap_of_word(_AGENT_CITY_WORD, box) < TEXT_LAYER_MIN_WORD_OVERLAP
    assert _overlap_of_word(_AGENT_VALUE_WORD, box) >= TEXT_LAYER_MIN_WORD_OVERLAP


def test_name_boxes_shrank_toward_their_address_field_not_away_from_it():
    """A cheaper sanity check alongside the word-level tests above: the RECTANGLES still
    overlap the address field below them by a hair (shipper_name's bottom, 0.0639, is still
    past shipper_address's top, 0.0600) - that's fine, since no WORD's own overlap crosses
    the threshold in either direction (the word-level tests are the real contract). What
    would be wrong is the edit going the other way, so pin the direction: each name box got
    SHORTER than it used to be, not taller."""
    old_bottom = {"shipper_name": 0.0672, "consignee_name": 0.1581, "agent_name": 0.2506}
    for field_id, was in old_bottom.items():
        now = _field(field_id).bbox[3]
        assert now < was, f"{field_id} bottom ({now}) did not shrink from the old value ({was})"
