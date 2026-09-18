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
        {"id": "awb_number", "label": "AWB Number", "bbox": [0.720, 0.012, 0.890, 0.040],
         "data_type": "string", "validation_regex": r"^\d{3}-?\s?\d{8}$", "confidence_floor": _AWB_FLOOR},
        {"id": "carrier", "label": "Carrier", "bbox": [0.575, 0.062, 0.750, 0.098],
         "data_type": "string", "validation_regex": r"^[A-Za-z ]{3,40}$", "confidence_floor": _AWB_FLOOR},

        # --- parties --------------------------------------------------------
        {"id": "shipper_name", "label": "Shipper Name", "bbox": [0.098, 0.046, 0.330, 0.062],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "shipper_address", "label": "Shipper Address", "bbox": [0.098, 0.060, 0.520, 0.102],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "consignee_name", "label": "Consignee Name", "bbox": [0.098, 0.138, 0.320, 0.157],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "consignee_address", "label": "Consignee Address", "bbox": [0.098, 0.150, 0.470, 0.204],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "agent_name", "label": "Issuing Agent", "bbox": [0.098, 0.228, 0.380, 0.244],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "agent_iata_code", "label": "Agent IATA Code", "bbox": [0.105, 0.288, 0.215, 0.310],
         "data_type": "string", "validation_regex": r"^[\d/\-]{6,20}$", "confidence_floor": _AWB_FLOOR},

        # --- routing --------------------------------------------------------
        {"id": "airport_of_departure", "label": "Airport of Departure", "bbox": [0.105, 0.319, 0.270, 0.341],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "airport_of_destination", "label": "Airport of Destination", "bbox": [0.096, 0.379, 0.170, 0.401],
         "data_type": "string", "validation_regex": r"^[A-Za-z .]{3,30}$", "confidence_floor": _AWB_FLOOR},
        {"id": "destination_code", "label": "Destination Code", "bbox": [0.098, 0.348, 0.142, 0.370],
         "data_type": "string", "validation_regex": r"^[A-Z]{3}$", "confidence_floor": _AWB_FLOOR},
        {"id": "first_carrier_code", "label": "First Carrier", "bbox": [0.142, 0.348, 0.175, 0.370],
         "data_type": "string", "validation_regex": r"^[A-Z0-9]{2}$", "confidence_floor": _AWB_FLOOR},
        {"id": "flight_date", "label": "Flight / Date", "bbox": [0.300, 0.379, 0.420, 0.401],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},

        # --- charge declarations --------------------------------------------
        {"id": "currency", "label": "Currency", "bbox": [0.520, 0.348, 0.562, 0.370],
         "data_type": "string", "validation_regex": r"^[A-Z]{3}$", "confidence_floor": _AWB_FLOOR},
        {"id": "charges_code", "label": "Charges Code", "bbox": [0.562, 0.348, 0.598, 0.370],
         "data_type": "string", "validation_regex": r"^[A-Z]{2}$", "confidence_floor": _AWB_FLOOR},
        {"id": "declared_value_carriage", "label": "Declared Value for Carriage", "bbox": [0.686, 0.348, 0.736, 0.370],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "declared_value_customs", "label": "Declared Value for Customs", "bbox": [0.833, 0.348, 0.882, 0.370],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "accounting_information", "label": "Accounting Information", "bbox": [0.530, 0.228, 0.690, 0.244],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},

        # --- cargo line -----------------------------------------------------
        {"id": "pieces_rcp", "label": "No. of Pieces (RCP)", "bbox": [0.098, 0.495, 0.130, 0.519],
         "data_type": "string", "validation_regex": r"^\d{1,4}$", "confidence_floor": _AWB_FLOOR},
        {"id": "gross_weight", "label": "Gross Weight", "bbox": [0.138, 0.495, 0.180, 0.519],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,10}$", "confidence_floor": _AWB_FLOOR},
        {"id": "rate_class", "label": "Rate Class", "bbox": [0.242, 0.495, 0.270, 0.519],
         "data_type": "enum", "validation_regex": r"^[A-Z]$", "confidence_floor": _AWB_FLOOR},
        {"id": "chargeable_weight", "label": "Chargeable Weight", "bbox": [0.350, 0.495, 0.392, 0.519],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,10}$", "confidence_floor": _AWB_FLOOR},
        {"id": "rate_charge", "label": "Rate / Charge", "bbox": [0.448, 0.495, 0.498, 0.519],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_charge", "label": "Total", "bbox": [0.558, 0.495, 0.628, 0.519],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "nature_of_goods", "label": "Nature and Quantity of Goods", "bbox": [0.705, 0.491, 0.960, 0.513],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "hs_code", "label": "HS Code", "bbox": [0.705, 0.516, 0.862, 0.538],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "invoice_reference", "label": "Invoice Reference", "bbox": [0.705, 0.529, 0.885, 0.551],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},

        # --- totals ---------------------------------------------------------
        {"id": "prepaid_weight_charge", "label": "Prepaid Weight Charge", "bbox": [0.098, 0.713, 0.162, 0.737],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_other_charges_agent", "label": "Total Other Charges Due Agent", "bbox": [0.098, 0.802, 0.168, 0.826],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_other_charges_carrier", "label": "Total Other Charges Due Carrier", "bbox": [0.098, 0.833, 0.175, 0.857],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},
        {"id": "total_prepaid", "label": "Total Prepaid", "bbox": [0.098, 0.896, 0.185, 0.920],
         "data_type": "string", "validation_regex": r"^[\d.,]{1,12}$", "confidence_floor": _AWB_FLOOR},

        # --- execution ------------------------------------------------------
        {"id": "executed_date", "label": "Executed On (Date)", "bbox": [0.432, 0.910, 0.552, 0.932],
         "data_type": "string", "validation_regex": None, "confidence_floor": _AWB_FLOOR},
        {"id": "executed_place", "label": "Executed At (Place)", "bbox": [0.615, 0.910, 0.735, 0.932],
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
