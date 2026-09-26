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

from app.schemas.template import TemplateIn  # noqa: E402

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

AIR_WAYBILL = TemplateIn(
    template_id="air_waybill",
    # 612x792pt US Letter. Supplying the true ratio stops the perspective warp
    # from having to infer it from foreshortened edges, which is what slid
    # field boxes off their cells on angled photos. ruled_form erases the
    # grid's printed borders, without which "65" reads as "Jos".
    hints={"page_aspect_ratio": 612 / 792, "ruled_form": True},
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
        {"id": "shipper_name", "label": "Shipper Name", "bbox": [0.1013, 0.0525, 0.3114, 0.0672],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "shipper_address", "label": "Shipper Address", "bbox": [0.098, 0.060, 0.520, 0.102],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        # Top edge 0.1425, not the 0.1395 the PDF text layer suggests: the printed label's descender
        # ("g" of "Consignee's Name and Address", label ink down to y=0.1395 at 200 DPI) reached 2 rows
        # into the box. The value's own ink starts at 0.1450, so 0.1425 is the middle of the gap. Measured by
        # replaying cached crops from the 19 OCR-path synthetic files: 16/19 -> 19/19 exact (0.1410 and
        # 0.1440 both give 18). The bottom edge is left alone - moving it as well dropped this to 10/19.
        {"id": "consignee_name", "label": "Consignee Name", "bbox": [0.1013, 0.1425, 0.2117, 0.1581],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "consignee_address", "label": "Consignee Address", "bbox": [0.098, 0.150, 0.470, 0.204],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "agent_name", "label": "Issuing Agent", "bbox": [0.1013, 0.2304, 0.3663, 0.2506],
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

ALL_TEMPLATES = [BITS_GOA_ID_CARD, AIR_WAYBILL]


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
