#!/usr/bin/env python
"""
Cause of every wrong field of a page-engine run on DEV (sprint Step 3). Counts only; dev only.

    python scripts/diagnose_page.py --results data/eval_real/results/page_qwen3vl_dev_base.json

One cause per wrong field, first match wins:
  label_suspect    the draft label itself is flagged uncertain in data/real_awb/labels (a person should check the label, not the reader)
  missing          the reader returned "" for a field that has a label
  spurious         the reader returned a value for a field whose label is blank
  field_confusion  the value equals the label of ANOTHER field of the same document
  format           same characters once punctuation / case / spaces are dropped, or one value contains the other
  small_text       a near miss (fuzzy >= 70): characters misread, typical of small or noisy print
  other            none of the above
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rapidfuzz import fuzz  # noqa: E402

from app.utils.compare import normalise, values_agree  # noqa: E402
from scripts.score_real import EVAL, load_split  # noqa: E402


def _uncertain() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in (ROOT / "data" / "real_awb" / "labels").glob("*.json"):
        if p.name[0].isdigit():
            out[p.name.split("_")[0]] = set(json.loads(p.read_text(encoding="utf-8")).get("uncertain", {}))
    return out


def cause(fid: str, got: str, want: str, doc_labels: dict[str, str], suspect: set[str]) -> str:
    if fid in suspect:
        return "label_suspect"
    if not got.strip():
        return "missing"
    if not want.strip():
        return "spurious"
    g = normalise(fid, got)
    if any(o != fid and w.strip() and normalise(o, w) == g for o, w in doc_labels.items()):
        return "field_confusion"
    squash = lambda s: re.sub(r"[^a-z0-9]", "", normalise(fid, s))  # noqa: E731
    gs, ws = squash(got), squash(want)
    if gs == ws or (ws and gs and (gs in ws or ws in gs)):
        return "format"
    if fuzz.ratio(g, normalise(fid, want)) >= 70:
        return "small_text"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True)
    args = ap.parse_args()
    results = json.loads(args.results.read_text(encoding="utf-8"))
    manifest, labels, _v = load_split("dev")
    unc = _uncertain()
    by_kind: dict[str, Counter] = defaultdict(Counter)
    by_field: dict[str, Counter] = defaultdict(Counter)
    for entry in manifest:
        res = results["docs"].get(entry["doc_id"], {})
        for fid, want in labels[entry["doc_id"]].items():
            f = (res.get("fields") or {}).get(fid)
            got = (f or {}).get("value", "")
            if f is not None and values_agree(fid, got, want):
                continue
            c = cause(fid, got, want, labels[entry["doc_id"]], unc.get(entry["doc_id"], set()))
            by_kind[entry["kind"]][c] += 1
            by_field[fid][c] += 1
    print("cause x kind:")
    for k, cnt in by_kind.items():
        print(f"  {k}: {dict(cnt.most_common())}  (total {sum(cnt.values())})")
    print("fields with the most misses:")
    for fid, cnt in sorted(by_field.items(), key=lambda kv: -sum(kv[1].values()))[:12]:
        print(f"  {fid}: {sum(cnt.values())} {dict(cnt.most_common())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
