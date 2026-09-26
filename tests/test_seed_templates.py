import re

from scripts.seed_templates import AIR_WAYBILL


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
