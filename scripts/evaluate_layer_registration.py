#!/usr/bin/env python
"""
Text-layer registration, before and after, on the digital documents of a labelled corpus (tesseract loop, iteration 1).

    PYTHONPATH=. python scripts/evaluate_layer_registration.py --docs-dir data/demo_50 --ground-truth data/demo_50/ground_truth.json

Runs inside the app container (it needs the seeded templates). Per document: Module 5 (no hint) -> the text-layer read with the template's boxes
as aligned, and again with each box moved by `layer_registration.register_boxes`; Module 7 on both; scored with `export_batch_json.same`. Prints
fields right, and wrong values per approval tier, for both. Truth is used only to score.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app import runtime  # noqa: E402

runtime.configure_tesseract()

from app.modules import field_extraction as fe  # noqa: E402
from app.modules import pipeline  # noqa: E402
from app.modules.layer_registration import register_boxes, shifted  # noqa: E402
from app.modules.preprocessing import preprocess_image  # noqa: E402
from app.modules.template_store import get_template_version  # noqa: E402
from app.modules.type_identification import identify_document_type  # noqa: E402
from app.modules.validation import demote_fields, validate_fields  # noqa: E402
from export_batch_json import same  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs-dir", type=Path)
    ap.add_argument("--ground-truth", type=Path)
    ap.add_argument("--real-dev", action="store_true", help="the dev-pool real waybills of data/real_awb (never the gold ones), with their draft labels; per-document lines")
    ap.add_argument("--nb", type=float)
    ap.add_argument("--gain", type=float)
    ap.add_argument("--soft", type=float)
    ap.add_argument("--step", type=float)
    ap.add_argument("--maxdx", type=float)
    args = ap.parse_args()
    from app.modules import layer_registration as lr
    for k, v in (("NEIGHBOURHOOD", args.nb), ("MIN_GAIN", args.gain), ("FORMAT_WEIGHT", args.soft), ("STEP", args.step), ("MAX_DX", args.maxdx)):
        if v is not None:
            setattr(lr, "TEXT_LAYER_REG_" + k, v)
    if args.real_dev:
        split = json.loads((ROOT / "data" / "gold_real" / "SPLIT.json").read_text(encoding="utf-8"))
        gold = {d["file"] for d in split["documents"]}
        labels = {p.name.split("_")[0]: p for p in (ROOT / "data" / "real_awb" / "labels").glob("*.json") if p.name[0].isdigit()}
        truth, paths = {}, {}
        for pdf in sorted((ROOT / "data" / "real_awb").glob("*/*.pdf")):
            if pdf.name in gold:
                continue
            for lab in (ROOT / "data" / "real_awb" / "labels").glob("*.json"):
                if lab.name[0].isdigit() and lab.name.split("_")[1][:11] == pdf.name[:11] and lab.name not in {d["label_file"] for d in split["documents"]}:
                    truth[pdf.name] = json.loads(lab.read_text(encoding="utf-8"))
                    paths[pdf.name] = pdf
    else:
        truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))
        paths = {n: args.docs_dir / n for n in truth}
    tally = {"base": Counter(), "reg": Counter()}
    moved = 0
    docs = 0
    for name in sorted(truth):
        path = paths[name]
        if not path.suffix.lower() == ".pdf":
            continue
        pages, _meta, layers = pipeline.prepare_document(path.read_bytes(), name, None)
        if layers[0] is None:
            continue
        generic = preprocess_image(pages[0], deskew=False)
        ident = identify_document_type(generic.image_bytes, None)
        if not ident.confirmed:
            continue
        template = get_template_version(ident.template_id, ident.version)
        docs += 1
        fields = fe._fields_on_page(template, ident.alignment)
        direct = [f for f in fields if f.from_field is None]
        shifts = register_boxes(layers[0], direct)
        moved += len(shifts)
        moved_fields = [f.model_copy(update={"bbox": shifted(f.bbox, shifts[f.id])}) if f.id in shifts else f for f in fields]
        for label, flds in (("base", fields), ("reg", moved_fields)):
            read = {f.id: fe.extract_field_from_text_layer(layers[0], f) for f in flds if f.from_field is None}
            ex = fe._apply_field_options(flds, read)
            summary = validate_fields(template, ex)
            if label == "reg":
                demote_fields(summary, shifts)  # as pipeline.process_page does
            res = {**summary.accepted, **summary.flagged}
            per = Counter()
            for fid, want in truth[name]["fields"].items():
                r = res.get(fid)
                if r is None:
                    continue
                ok = same(fid, r.value, want)
                tier = r.approval_tier
                tally[label]["fields"] += 1
                tally[label]["right"] += ok
                tally[label][f"tier_{tier}"] += 1
                per["right"] += ok
                per["wrong_auto"] += (not ok) and tier == "auto"
                if not ok:
                    tally[label][f"wrong_{tier}"] += 1
            if args.real_dev:
                print(f"  {name[:11]} {label}: right {per['right']}/{len(truth[name]['fields'])} wrong_auto {per['wrong_auto']}")
    print(f"digital documents {docs}; boxes moved {moved}")
    for label in ("base", "reg"):
        print(label, dict(tally[label]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
