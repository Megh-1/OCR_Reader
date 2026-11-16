#!/usr/bin/env python
"""
Honest scorer for real documents (sprint Step 2). Totals only; never a document, field or value.

    python scripts/score_real.py --results R.json [--split dev] [--name LABEL] [--append docs/reader-results.md]

R.json: {"docs": {doc_id: {"status": "ok"|"refused"|"failed", "template_id": str|null, "fields": {field_id: {"value", "tier", ...}}}}}
Denominator = EVERY document of the split x all 33 field ids. A missing document, a refused page, a failed run, a field missing from the result
and a wrong document type (an ID card) all count as WRONG. Comparison: app/utils/compare.py (strip, collapse whitespace, case-insensitive;
numbers as numbers; leading printed label dropped for hs_code / invoice_reference) and nothing else.

Per input kind (digital / scan / photo): fields right, % at auto, wrong at auto, right-but-flagged, hallucinations (a non-empty value that
matches nothing on the page's text layer or whole-page Tesseract words, fuzzy < 80). The first line is "LABELS UNVERIFIED" unless every label
row carries verified_by. The test split is refused unless scripts/run_final_test.py calls score() itself.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import runtime  # noqa: E402

runtime.configure_tesseract()

from app.modules.grounding import HALLUCINATION_FUZZY, PageText, page_text  # noqa: E402
from app.modules.ingestion import extract_text_layers  # noqa: E402
from app.utils.compare import values_agree  # noqa: E402

EVAL = ROOT / "data" / "eval_real"
AWB_TEMPLATES = {None, "air_waybill", "air_waybill_bkk", "air_waybill_routing"}
KINDS = ("digital", "scan", "photo")


def load_split(split: str) -> tuple[list[dict], dict[str, dict[str, str]], bool]:
    d = EVAL / split
    manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    labels: dict[str, dict[str, str]] = defaultdict(dict)
    verified = True
    with (d / "labels.csv").open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            labels[row["doc_id"]][row["field_id"]] = row["value"]
            verified &= bool(row["verified_by"].strip())
    return manifest, labels, verified


def _page_text(split: str, entry: dict) -> PageText:
    """Text-layer words + whole-page Tesseract words of page 1, cached so a re-score costs nothing."""
    cache = EVAL / split / "_pagetext" / f"{entry['doc_id']}.json"
    if cache.exists():
        c = json.loads(cache.read_text(encoding="utf-8"))
        return PageText(layer=c["layer"], ocr=c["ocr"])
    d = EVAL / split
    data = (d / entry["file"]).read_bytes()
    layers = extract_text_layers(data) if entry["file"].endswith(".pdf") else []
    pt = page_text((d / f"{entry['doc_id']}.png").read_bytes(), layers[0] if layers else None)
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps({"layer": pt.layer, "ocr": pt.ocr}), encoding="utf-8")
    return pt


def score(results: dict, split: str, *, _final_test: bool = False) -> dict:
    if split == "test" and not _final_test:
        raise SystemExit("data/eval_real/test is spent only by scripts/run_final_test.py")
    manifest, labels, verified = load_split(split)
    docs = results.get("docs", {})
    tot = {k: defaultdict(int) for k in KINDS}
    for entry in manifest:
        t = tot[entry["kind"]]
        t["documents"] += 1
        res = docs.get(entry["doc_id"])
        usable = bool(res) and res.get("status") == "ok" and res.get("template_id") in AWB_TEMPLATES
        pt = _page_text(split, entry) if usable else None
        for fid, want in labels[entry["doc_id"]].items():
            t["fields"] += 1  # the denominator: every document x every field
            f = (res or {}).get("fields", {}).get(fid) if usable else None
            if f is None:
                continue  # wrong (never counted right)
            got = f.get("value") or ""
            tier = f.get("tier")
            right = values_agree(fid, got, want)
            t["right"] += right
            if tier == "auto":
                t["auto"] += 1
                t["wrong_auto"] += not right
            if right and tier != "auto":
                t["right_flagged"] += 1
            if got.strip() and pt.best_score(got) < HALLUCINATION_FUZZY:
                t["hallucinations"] += 1
    return {"labels_verified": verified, "by_kind": {k: dict(v) for k, v in tot.items() if v["documents"]}}


def table(name: str, sc: dict) -> str:
    lines = [] if sc["labels_verified"] else ["LABELS UNVERIFIED", ""]
    lines.append(f"| {name} | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    allk: dict[str, int] = defaultdict(int)
    rows = list(sc["by_kind"].items())
    for _k, v in rows:
        for a, b in v.items():
            allk[a] += b
    for k, v in rows + [("all", dict(allk))]:
        g = lambda key: v.get(key, 0)  # noqa: E731
        f = g("fields") or 1
        lines.append(f"| | {k} | {g('documents')} | {g('fields')} | {g('right')} | {100 * g('right') / f:.1f} | {g('auto')} | {100 * g('auto') / f:.1f} "
                     f"| {g('wrong_auto')} | {g('right_flagged')} | {g('hallucinations')} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--split", default="dev")
    ap.add_argument("--name", default=None)
    ap.add_argument("--append", type=Path, default=None)
    args = ap.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    out = table(args.name or results.get("config", "run"), score(results, args.split))
    print(out)
    if args.append:
        with args.append.open("a", encoding="utf-8") as fh:
            fh.write(f"\n### {args.name or results.get('config', 'run')} ({args.split})\n\n{out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
