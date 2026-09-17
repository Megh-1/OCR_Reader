"""
Creates the accuracy_harness_demo template these demo images test against.
Idempotent - safe to run more than once, safe to run against whichever
database DATABASE_URL currently points at.

Run once before evaluate_accuracy.py against scripts/demo_data/.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.db import SessionLocal
from app.modules.template_store import create_template_version, get_latest_template
from app.schemas.template import TemplateIn

DOC_TYPE = "accuracy_harness_demo"

if get_latest_template(DOC_TYPE) is not None:
    print(f"{DOC_TYPE} already exists - nothing to do")
else:
    session = SessionLocal()
    template = create_template_version(
        session,
        TemplateIn(
            template_id=DOC_TYPE,
            anchors=[{"text": "DEMO CARD", "bbox": (0.03, 0.03, 0.40, 0.18), "match_threshold": 0.8}],
            fields=[
                {
                    "id": "person_name",
                    "label": "Name",
                    "bbox": (0.03, 0.36, 0.65, 0.49),
                    "data_type": "string",
                    "validation_regex": None,
                    "confidence_floor": 0.5,
                }
            ],
        ),
    )
    session.close()
    print(f"created {DOC_TYPE} v{template.version}")
