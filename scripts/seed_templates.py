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

ALL_TEMPLATES = [BITS_GOA_ID_CARD]


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
