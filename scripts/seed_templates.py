"""
Canonical template definitions, and a CLI to load them into a database.

These exist as code rather than as rows someone once created by hand so a
template can be rebuilt exactly - on a fresh machine, in a container, in the
test database, or after a reset. The Template Designer UI remains the normal
way to author a NEW document type; this file is the checked-in record of the
ones already validated against real documents.

    python scripts/seed_templates.py                  # seed configured DATABASE_URL
    python scripts/seed_templates.py --list           # show what's defined here

Templates are append-only (Module 4), so seeding an existing doc_type adds a
new version rather than editing one - re-running is safe but not a no-op.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas.template import TemplateCheck, TemplateIn  # noqa: E402
from scripts.waybill_forms import AIR_WAYBILL_BKK, landmarks as _landmarks  # noqa: E402  (one loader for both files)


# --- bits_goa_id_card -------------------------------------------------------
#
# Every value below was measured against a real photo of the card, not chosen
# by eye. The two non-obvious ones:
#
# match_threshold 0.70 - the correct card scores ~86 and unrelated documents
#   score ~0, so the threshold sits in a wide empty gap. Pinning it just under
#   the match score (it was 0.85) left ~1 point of tolerance for lighting and
#   angle, which is the reference doc's "legitimate document flagged as
#   unknown" risk.
#
# date_of_birth's month is enumerated, not shape-matched. A shape-only pattern
#   (^\d{2}-[A-Z][a-z]{2}-\d{4}$) accepted the OCR misread "17-Oci-2006" as
#   valid, shipping a wrong value as trusted output.
BITS_GOA_ID_CARD = TemplateIn(
    template_id="bits_goa_id_card",
    anchors=[
        {
            "text": "Birla Institute of Technology & Science, Pilani",
            "bbox": [0.2351421188630491, 0.1018867924528302, 0.9589147286821705, 0.17547169811320754],
            "match_threshold": 0.70,
        },
    ],
    fields=[
        {
            "id": "student_name",
            "label": "Student Name",
            "bbox": [0.30981912144702844, 0.33132075471698114, 0.6237726098191214, 0.3969811320754717],
            "data_type": "string",
            "validation_regex": r"^[A-Za-z ]{2,60}$",
            "confidence_floor": 0.75,
        },
        {
            "id": "id_number",
            "label": "Id Number",
            "bbox": [0.4170542635658915, 0.45547169811320753, 0.689405684754522, 0.5090566037735849],
            "data_type": "string",
            "validation_regex": r"^\d{4}[A-Z]\d[A-Z]{2}\d{4}[A-Z]$",
            "confidence_floor": 0.75,
        },
        {
            "id": "date_of_birth",
            "label": "Date of Birth",
            "bbox": [0.5157622739018087, 0.5769811320754717, 0.7126614987080103, 0.6313207547169811],
            "data_type": "string",
            "validation_regex": r"^\d{2}-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-\d{4}$",
            "confidence_floor": 0.75,
        },
    ],
)

# --- air_waybill -------------------------------------------------------------
#
# IATA air waybill. Field boxes were derived from the sample PDF's own text
# layer (exact coordinates in page space), not drawn by eye, then padded for
# the coordinate drift a perspective warp leaves behind - the warp restores a
# photographed page's geometry from measured edge lengths, which are
# foreshortened, so recovered aspect ratio is close but not exact.
#
# Both anchors are PRINTED FORM LABELS, never shipment data. That distinction
# is what makes the template reusable across air waybills instead of matching
# only this one shipment: a region like "Consignee's Name and Address" reads
# beautifully and stably, but it contains the consignee, so anchoring on it
# would reject every other AWB. They are also at opposite ends of the page,
# so neither can bleed into the other the way two adjacent anchors can.
#
# Anchor text is what Tesseract actually returns for that crop across clean
# and distorted renderings, not what a human reads off the form.
_AWB_FLOOR = 0.60  # photographed forms read lower than a flat scan; see notes below

# Cross-field checks (app/modules/checks.py). Each one is a fact about air waybills, not about the
# one reference document, and each only ADDS caution: a failure flags every field it names.
_AWB_CHECKS = [
    # Rating: chargeable weight x rate = total charge. Only for class Q (normal rate); a minimum
    # charge (M), a specific commodity rate (C) and others are not a plain product.
    TemplateCheck(
        id="total_equals_weight_times_rate", label="Total charge = chargeable weight x rate",
        kind="product", inputs=["chargeable_weight", "rate_charge"], target="total_charge",
        tolerance=0.01, when={"field": "rate_class", "equals": "Q"},
    ),
    # The flight number starts with the airline's 2-character code, which is also the first carrier.
    TemplateCheck(
        id="flight_prefix_matches_first_carrier", label="Flight prefix = first carrier code",
        kind="equal", inputs=["flight_date", "first_carrier_code"],
        patterns={"flight_date": r"^([A-Z0-9]{2})\d{1,4}/"},
    ),
    TemplateCheck(
        id="currency_is_iso_4217", label="Currency is a real ISO 4217 code",
        kind="one_of", inputs=["currency"], lookup="iso4217",
    ),
    # IATA airline prefix -> carrier. Only 176 is listed because it is the only pair we have a
    # document for; a prefix not in the table is skipped, so other carriers are not blocked.
    # Add pairs as real documents confirm them.
    TemplateCheck(
        id="awb_prefix_matches_carrier", label="AWB prefix matches the carrier",
        kind="maps_to", inputs=["awb_number"], target="carrier",
        patterns={"awb_number": r"^(\d{3})"}, table={"176": "EMIRATES"},
    ),
]

AIR_WAYBILL = TemplateIn(
    template_id="air_waybill",
    # 612x792pt US Letter. Supplying the true ratio stops the perspective warp
    # from having to infer it from foreshortened edges, which is what slid
    # field boxes off their cells on angled photos. ruled_form erases the
    # grid's printed borders, without which "65" reads as "Jos".
    hints={"page_aspect_ratio": 612 / 792, "ruled_form": True},
    checks=_AWB_CHECKS,
    landmarks=_landmarks("air_waybill"),
    anchors=[
        {
            "text": "Not Negotiable Air Waybill",
            "bbox": [0.540, 0.036, 0.612, 0.053],
            "match_threshold": 0.70,
        },
        {
            "text": "Nature and Quantity of Goods (incl. Dimensions or Volume)",
            "bbox": [0.752, 0.464, 0.900, 0.480],
            "match_threshold": 0.70,
        },
    ],
    fields=[
        # --- identity -------------------------------------------------------
        {"id": "awb_number", "label": "AWB Number", "bbox": [0.7304, 0.0158, 0.8756, 0.0374],
         "data_type": "string", "validation_regex": r"^\d{3}-?\s?\d{8}$", "confidence_floor": _AWB_FLOOR,
         # The regex only says an AWB number is shaped right; the IATA mod-7 check digit
         # catches one that is shaped right and wrong (176-06067726 read for 176-06067736).
         "checksum": "iata_awb_mod7"},
        {"id": "carrier", "label": "Carrier", "bbox": [0.5899, 0.0663, 0.7325, 0.0941],
         "data_type": "string", "validation_regex": r"^[A-Za-z ]{3,40}$", "confidence_floor": _AWB_FLOOR},

        # --- parties --------------------------------------------------------
        # Top edge 0.0525, not the 0.0486 the PDF text layer suggests: on the RASTER path the printed
        # label "Shipper's Name and Address" (ink to y=0.0509 at 200 DPI) hung 6 rows into the box, and
        # Tesseract read it into the value. The value's own ink starts at 0.0545, so 0.0525 is the middle of
        # that 8 px gap. Measured on the synthetic corpus: 8/19 -> 16/19 exact.
        #
        # Bottom edge 0.0639, not the original 0.0672: that overlapped shipper_address's own top
        # (0.0600) by 0.0072, and extract_field_from_text_layer's word-overlap check is per field with
        # no cross-field exclusivity, so a word straddling the shared band counts toward both - the
        # address's first line ("SURVEY NO.13/1, KANEKAL VILLAGE") was appended to shipper_name's value,
        # even on the pure text-layer path at 100% confidence (no OCR, so not a misread - a box geometry
        # bug). Measured at 200 DPI on reference__original_digital.pdf: value ink ends 0.0623, address's
        # first line begins 0.0655; 0.0639 is the middle of that gap. Verified directly against
        # field_extraction._overlap_of_word on the real text layer: at 0.0639 the address's first word
        # scores 0.062 (was 0.331 at the old edge - the 0.5 threshold was never actually this close, the
        # word's own bbox is just taller than its ink), while the value's own words still score 0.929
        # (barely down from 0.941) - the fix removes the bleed without touching the value's own text.
        {"id": "shipper_name", "label": "Shipper Name", "bbox": [0.1013, 0.0525, 0.3114, 0.0639],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "shipper_address", "label": "Shipper Address", "bbox": [0.098, 0.060, 0.520, 0.102],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        # Top edge 0.1425, not the 0.1395 the PDF text layer suggests: the printed label's descender
        # ("g" of "Consignee's Name and Address", label ink down to y=0.1395 at 200 DPI) reached 2 rows
        # into the box. The value's own ink starts at 0.1450, so 0.1425 is the middle of the gap. Measured by
        # replaying cached crops from the 19 OCR-path synthetic files: 16/19 -> 19/19 exact (0.1410 and
        # 0.1440 both give 18).
        #
        # Bottom edge 0.1548, not the original 0.1581 (moving the top further per the 2024 note above
        # dropped that first measurement to 10/19, but this is a DIFFERENT edge, fixing a different bug
        # found later: 0.1581 overlapped consignee_address's own top (0.1500) by 0.0081, so the address's
        # first line ("13-C, PLOT NO. 532-242,") bled into consignee_name the same way shipper_name did -
        # same root cause, same fix. Measured at 200 DPI: value ink ends 0.1532, address's first line
        # begins 0.1564; 0.1548 is the middle. Verified against _overlap_of_word: the address's first word
        # drops from 0.330 to 0.062, the value's own words stay at 0.988 (barely down from 1.000).
        {"id": "consignee_name", "label": "Consignee Name", "bbox": [0.1013, 0.1425, 0.2117, 0.1548],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "consignee_address", "label": "Consignee Address", "bbox": [0.098, 0.150, 0.470, 0.204],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        # Bottom edge 0.2473, not the original 0.2506: unlike the two pairs above, there is no separate
        # "agent address" field to blame - this box was simply taller than the one line of agent-name
        # text it was meant to hold, and swallowed the printed city line below it ("BANGALORE") on the
        # same text-layer, no-OCR, 100%-confidence path. Measured at 200 DPI: value ink ends 0.2455, the
        # city line begins 0.2491; 0.2473 is the middle. Verified against _overlap_of_word: "BANGALORE"
        # drops from 0.317 to 0.078, the agent name's own words stay at 0.993 (barely down from 1.000).
        {"id": "agent_name", "label": "Issuing Agent", "bbox": [0.1013, 0.2304, 0.3663, 0.2473],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "agent_iata_code", "label": "Agent IATA Code", "bbox": [0.1095, 0.2898, 0.2073, 0.3099],
         "data_type": "string", "validation_regex": r"^[\d/\-]{6,20}$", "confidence_floor": _AWB_FLOOR},

        # --- routing --------------------------------------------------------
        {"id": "airport_of_departure", "label": "Airport of Departure", "bbox": [0.1095, 0.3213, 0.2581, 0.3415],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "airport_of_destination", "label": "Airport of Destination", "bbox": [0.098, 0.3819, 0.1535, 0.4013],
         "data_type": "string", "validation_regex": r"^[A-Za-z .]{3,30}$", "confidence_floor": _AWB_FLOOR},
        {"id": "destination_code", "label": "Destination Code", "bbox": [0.0997, 0.3504, 0.1409, 0.3705],
         "data_type": "string", "validation_regex": r"^[A-Z]{3}$", "confidence_floor": _AWB_FLOOR},
        {"id": "first_carrier_code", "label": "First Carrier", "bbox": [0.1422, 0.3504, 0.1714, 0.3705],
         "data_type": "string", "validation_regex": r"^[A-Z0-9]{2}$", "confidence_floor": _AWB_FLOOR},
        # Carrier designator, 4-digit flight number, "/", day, month: "EK0565/27FEB". The only wrong value Module 7
        # accepted in this field is "EKO565/27FEB" (letter O for zero), on several synthetic photos and a scan, at up
        # to confidence 87 - the most confident wrong read on the whole corpus - with nothing but confidence to stop it.
        # As strict as the data: a letters-only designator and a zero-padded number because that is what this form
        # carries, and an enumerated month (a shape-only one let "17-Oci-2006" through on the ID card). A carrier like
        # 6E or an unpadded "EK565" is flagged, not misread - widen it in a new version when a real waybill shows one.
        {"id": "flight_date", "label": "Flight / Date", "bbox": [0.3072, 0.3819, 0.4148, 0.4005],
         "data_type": "string",
         "validation_regex": r"^[A-Z]{2}\d{4}/(0[1-9]|[12]\d|3[01])(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)$",
         "confidence_floor": _AWB_FLOOR},

        # --- charge declarations --------------------------------------------
        {"id": "currency", "label": "Currency", "bbox": [0.5229, 0.3504, 0.5601, 0.3705],
         "data_type": "string", "validation_regex": r"^[A-Z]{3}$", "confidence_floor": _AWB_FLOOR},
        {"id": "charges_code", "label": "Charges Code", "bbox": [0.5637, 0.3504, 0.5913, 0.3705],
         "data_type": "string", "validation_regex": r"^[A-Z]{2}$", "confidence_floor": _AWB_FLOOR},
        {"id": "declared_value_carriage", "label": "Declared Value for Carriage", "bbox": [0.6895, 0.3504, 0.7317, 0.3705],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "declared_value_customs", "label": "Declared Value for Customs", "bbox": [0.8366, 0.3504, 0.8777, 0.3705],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "accounting_information", "label": "Accounting Information", "bbox": [0.5343, 0.2304, 0.6797, 0.2506],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},

        # --- cargo line -----------------------------------------------------
        {"id": "pieces_rcp", "label": "No. of Pieces (RCP)", "bbox": [0.1013, 0.4981, 0.1204, 0.5182],
         "data_type": "string", "validation_regex": r"^\d{1,4}$", "confidence_floor": _AWB_FLOOR},
        {"id": "gross_weight", "label": "Gross Weight", "bbox": [0.1422, 0.4981, 0.1706, 0.5182],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,10}$", "confidence_floor": _AWB_FLOOR},
        {"id": "rate_class", "label": "Rate Class", "bbox": [0.2444, 0.4981, 0.2657, 0.5182],
         "data_type": "enum", "validation_regex": r"^[A-Z]$", "confidence_floor": _AWB_FLOOR},
        {"id": "chargeable_weight", "label": "Chargeable Weight", "bbox": [0.3546, 0.4981, 0.3831, 0.5182],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,10}$", "confidence_floor": _AWB_FLOOR},
        {"id": "rate_charge", "label": "Rate / Charge", "bbox": [0.4526, 0.4981, 0.4904, 0.5182],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_charge", "label": "Total", "bbox": [0.5637, 0.4981, 0.6202, 0.5182],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "nature_of_goods", "label": "Nature and Quantity of Goods", "bbox": [0.7092, 0.4943, 0.956, 0.5129],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "hs_code", "label": "HS Code", "bbox": [0.705, 0.516, 0.862, 0.538],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "invoice_reference", "label": "Invoice Reference", "bbox": [0.705, 0.529, 0.885, 0.551],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},

        # --- totals ---------------------------------------------------------
        {"id": "prepaid_weight_charge", "label": "Prepaid Weight Charge", "bbox": [0.1013, 0.7165, 0.1578, 0.7367],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_other_charges_agent", "label": "Total Other Charges Due Agent", "bbox": [0.1013, 0.8049, 0.1631, 0.825],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_other_charges_carrier", "label": "Total Other Charges Due Carrier", "bbox": [0.1013, 0.8365, 0.1725, 0.8566],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_prepaid", "label": "Total Prepaid", "bbox": [0.1013, 0.8971, 0.1818, 0.9172],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},

        # --- execution ------------------------------------------------------
        {"id": "executed_date", "label": "Executed On (Date)", "bbox": [0.4363, 0.9123, 0.5418, 0.9324],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "executed_place", "label": "Executed At (Place)", "bbox": [0.6225, 0.9123, 0.7242, 0.9324],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
    ],
)

# A deliberately partial read of the same layout: only the routing and currency block.
# Its field boxes are the air_waybill ones (taken from it, so the two cannot drift apart).
# hint_only keeps it out of Module 5's no-hint scan, where its anchors would pass on the
# same pages as air_waybill's and turn every un-hinted upload into an ambiguous_match; it
# is used by choosing it as the type hint. Its anchors still have to pass on that page.
_ROUTING_FIELD_IDS = (
    "awb_number", "airport_of_departure", "destination_code", "first_carrier_code",
    "airport_of_destination", "flight_date", "currency", "charges_code",
)
AIR_WAYBILL_ROUTING = TemplateIn(
    template_id="air_waybill_routing",
    hints={**AIR_WAYBILL.hints.model_dump(), "hint_only": True},
    landmarks=AIR_WAYBILL.landmarks,  # same layout, same page frame
    anchors=[
        {
            "text": "Airport of Departure (Addr. of First Carrier) and Requested Routing",
            "bbox": [0.100, 0.3104, 0.420, 0.3242],
            "match_threshold": 0.70,
        },
        {
            "text": "Issuing Carrier's Agent Name and City",
            "bbox": [0.1003, 0.2213, 0.2588, 0.2300],
            "match_threshold": 0.70,
        },
    ],
    fields=[f for f in AIR_WAYBILL.fields if f.id in _ROUTING_FIELD_IDS],
    # a check is kept only when every field it needs is in this template (TemplateIn refuses the rest)
    checks=[
        c for c in AIR_WAYBILL.checks
        if {*c.field_ids(), *c.patterns, *([c.when.field] if c.when else [])} <= set(_ROUTING_FIELD_IDS)
    ],
)

ALL_TEMPLATES = [BITS_GOA_ID_CARD, AIR_WAYBILL, AIR_WAYBILL_ROUTING, AIR_WAYBILL_BKK]


def seed(templates=ALL_TEMPLATES) -> list[tuple[str, int]]:
    """Inserts each template as a new version. Returns (template_id, version) pairs."""
    from app.db import SessionLocal
    from app.modules.template_store import create_template_version

    written = []
    with SessionLocal() as session:
        for template in templates:
            out = create_template_version(session, template)
            written.append((out.template_id, out.version))
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the defined templates and exit")
    args = parser.parse_args()

    if args.list:
        for template in ALL_TEMPLATES:
            print(f"{template.template_id}: {len(template.anchors)} anchor(s), {len(template.fields)} field(s)")
            for field in template.fields:
                print(f"    {field.id:15} {field.data_type:8} floor={field.confidence_floor}  {field.validation_regex}")
        return

    print(f"seeding {os.environ.get('DATABASE_URL', '(default from app.config)')}")
    for template_id, version in seed():
        print(f"  wrote {template_id} v{version}")


if __name__ == "__main__":
    main()
