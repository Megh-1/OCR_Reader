"""
Measures a waybill template against the labelled real files, through the real pipeline functions
(alignment -> anchors -> Module 6 -> Module 7), with the template held in memory: nothing is written
to the database.

    python scripts/evaluate_forms.py air_waybill_bkk [--forms B] [--only 08 11] [--show-wrong]

Reads data/real_awb/labels/*.json (untracked: real third-party data). A label file names its PDF, a
`form` letter (B, M, S, C) and the field values. Per file it prints how the page was recognised,
and for every field whether the read matched the label and what Module 7 did with it:

    right+auto / right+spot / right+review      the value matched; how much human attention it still got
    WRONG+auto / WRONG+spot / WRONG+review      the value did not match; WRONG+auto is the dangerous one

`nature_of_goods` is free text whose end depends on where the description stops. A read that begins with the
label but runs on is counted separately as `prefix+auto/spot/review` - neither right nor wrong, an over-capture
to look at - so it can never inflate the right count.
"""

from __future__ import annotations

import argparse
import glob
import importlib
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import TESSERACT_CMD  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

from app.modules import alignment as alignment_mod, field_extraction, pipeline, preprocessing  # noqa: E402
from app.modules import type_identification as ti  # noqa: E402
from app.modules.validation import validate_fields  # noqa: E402
from app.schemas.template import TemplateLandmark, TemplateOut  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "data" / "real_awb"
PREFIX_OK = {"nature_of_goods"}


def norm(s: str) -> str:
    return " ".join((s or "").split())


def load_labels(forms: set[str], only: list[str] | None):
    out = []
    for path in sorted((ROOT / "labels").glob("*.json")):
        if only and not any(path.name.startswith(o) for o in only):
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["form"][:1] in forms:
            out.append((path.name, doc))
    return out


def find_pdf(name: str) -> Path:
    return next(p for p in (ROOT).glob("*/*.pdf") if p.name == name)


def evaluate(template_in, labels, show_wrong: bool):
    template = TemplateOut(version=1, created_at=date.today(), **template_in.model_dump(mode="json"))
    if os.environ.get("IDP_LANDMARKS"):  # e.g. a list built from the training pages only, so a held-out page is not aligned by its own words
        lm = json.loads(Path(os.environ["IDP_LANDMARKS"]).read_text(encoding="utf-8"))
        template = template.model_copy(update={"landmarks": [TemplateLandmark(**d) for d in lm]})
    field_extraction.get_template_version = lambda *a, **k: template  # in memory: no database
    totals, per_field = Counter(), {}
    for fname, doc in labels:
        pdf = find_pdf(doc["file"])
        pages, meta, layers = pipeline.prepare_document(pdf.read_bytes(), pdf.name, None)
        generic = preprocessing.preprocess_image(pages[0], deskew=layers[0] is None)
        image = Image.open(io.BytesIO(generic.image_bytes))
        image.load()
        al = alignment_mod.fit_alignment(template.landmarks, alignment_mod.read_page_words(image)) if template.landmarks else None
        ok, scores = ti._check_template(image, template, al) if al else ti._check_template(image, template)
        head = f"{fname:42s} {'native' if layers[0] else 'scan  '} anchors {[round(v) for v in scores.values()]} landmarks {al.landmarks if al else 0:3d}"
        if not ok:
            print(f"{head}  REFUSED (page not recognised as this form)")
            totals["refused"] += 1
            continue
        extraction = pipeline.extract_page_fields(template, pages[0], generic, layers[0], al)
        summary = validate_fields(template, extraction.fields)
        results = {**summary.accepted, **summary.flagged}
        c = Counter()
        wrong_lines = []
        for fid, want in doc["fields"].items():
            if fid not in results:  # a partial template (e.g. the routing one) has only some of the labelled fields
                continue
            got = extraction.fields[fid].raw_text if fid in extraction.fields else ""
            tier = results[fid].approval_tier
            short = {"auto": "auto", "spot_check": "spot", "review": "review"}[tier]
            pf = per_field.setdefault(fid, Counter())
            if norm(got) == norm(want):
                c[f"right+{short}"] += 1
                pf["right"] += 1
            elif fid in PREFIX_OK and want and norm(got).startswith(norm(want)):
                # begins with the label but runs on: neither right nor wrong - an over-capture a person has to look at
                c[f"prefix+{short}"] += 1
                pf["prefix"] += 1
                wrong_lines.append((fid, norm(want)[:48], norm(got)[:48] + "  [prefix]", tier))
            else:
                c[f"WRONG+{short}"] += 1
                pf["wrong"] += 1
                wrong_lines.append((fid, norm(want)[:48], norm(got)[:48], tier))
        totals.update(c)
        totals["docs"] += 1
        print(f"{head}  right {sum(v for k, v in c.items() if k.startswith('right'))}/{len(doc['fields'])}   "
              f"auto {c['right+auto']}  spot {c['right+spot']}  review-but-right {c['right+review']}   "
              f"prefix {c['prefix+auto'] + c['prefix+spot'] + c['prefix+review']}  WRONG: auto {c['WRONG+auto']} spot {c['WRONG+spot']} review {c['WRONG+review']}")
        if show_wrong:
            for fid, want, got, tier in wrong_lines:
                print(f"       {tier:10s} {fid:28s} want {want!r:50s} got {got!r}")
    n_right = sum(v for k, v in totals.items() if k.startswith("right"))
    n_wrong = sum(v for k, v in totals.items() if k.startswith("WRONG"))
    n_prefix = sum(v for k, v in totals.items() if k.startswith("prefix"))
    print(f"\nTOTAL over {totals['docs']} recognised, {totals['refused']} refused: {n_right} right, {n_wrong} wrong, {n_prefix} prefix (over-captured) "
          f"| right+auto {totals['right+auto']}, right+spot {totals['right+spot']}, right+review {totals['right+review']} "
          f"| WRONG+auto {totals['WRONG+auto']}, WRONG+spot {totals['WRONG+spot']}, WRONG+review {totals['WRONG+review']}")
    return per_field


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("template", help="template variable name in scripts/waybill_forms.py, lower-case id e.g. air_waybill_bkk")
    ap.add_argument("--forms", default="B", help="label form letters to evaluate, e.g. B or BS")
    ap.add_argument("--only", nargs="*", help="label file number prefixes, e.g. 08 11")
    ap.add_argument("--show-wrong", action="store_true")
    args = ap.parse_args()
    forms = importlib.import_module("scripts.waybill_forms")
    seeded = importlib.import_module("scripts.seed_templates").ALL_TEMPLATES  # the Mumbai-printer `air_waybill` lives there
    template_in = next(t for t in [*forms.ALL, *seeded] if t.template_id == args.template)
    per_field = evaluate(template_in, load_labels(set(args.forms), args.only), args.show_wrong)
    print("\nper field (right / total):")
    for fid, c in per_field.items():
        print(f"  {fid:28s} {c['right']}/{c['right'] + c['wrong'] + c['prefix']}")


if __name__ == "__main__":
    main()
