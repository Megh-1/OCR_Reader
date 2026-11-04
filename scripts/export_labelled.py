"""
Exports the values people have settled (labelled_examples) as a corpus scripts/evaluate_reader.py can score engines on.

    python scripts\\export_labelled.py --template air_waybill_bkk --out data\\labelled_bkk [--min-fields 1]

Writes `<out>/result_<id>.jpg` (the page image the reviewer saw, stored with the extraction result) and `<out>/ground_truth.json` in the
format evaluate_accuracy.py reads, holding ONLY the fields a person settled for that page (a page's other fields have no label, so they
are not scored). Then:

    python scripts\\evaluate_reader.py --images-dir data\\labelled_bkk --ground-truth data\\labelled_bkk\\ground_truth.json \\
        --engine tesseract --engine NAME --save --promote

Limits worth knowing: the stored image is the review-sized JPEG render, not the original upload, so it is a somewhat softer page than the
engine saw in production; and the labelled fields are the ones that needed a person, i.e. the HARD ones, so accuracy on this corpus runs
lower than on the whole page. That is the right corpus for choosing between engines, and the wrong one for quoting a headline figure.
The images are real business documents: the output folder stays out of git (data/ is ignored) and off shared drives.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models.engine_selection import LabelledExampleRecord  # noqa: E402
from app.models.extraction_result import ExtractionResultRecord  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--template", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--min-fields", type=int, default=1, help="skip pages with fewer settled fields than this")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    ground_truth: dict[str, dict] = {}
    with SessionLocal() as session:
        rows = session.execute(select(LabelledExampleRecord).where(LabelledExampleRecord.template_id == args.template)).scalars().all()
        by_result: dict[int, list] = defaultdict(list)
        for r in rows:
            by_result[r.extraction_result_id].append(r)
        for result_id, labels in sorted(by_result.items()):
            if len(labels) < args.min_fields:
                continue
            record = session.get(ExtractionResultRecord, result_id)
            if record is None or record.page_image is None:
                continue
            name = f"result_{result_id}.jpg"
            (args.out / name).write_bytes(record.page_image)
            ground_truth[name] = {"template_id": args.template, "version": labels[0].template_version, "tier": "labelled_render",
                                  "fields": {l.field_id: l.value for l in labels}}
    (args.out / "ground_truth.json").write_text(json.dumps(ground_truth, indent=1), encoding="utf-8")
    fields = sum(len(g["fields"]) for g in ground_truth.values())
    print(f"{len(ground_truth)} pages, {fields} labelled fields -> {args.out}")
    if not ground_truth:
        print("nothing to export: no value of this template has been settled one at a time yet", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
