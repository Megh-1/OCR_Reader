#!/usr/bin/env python
"""
Scores the GOLD real waybills (data/gold_real) THROUGH a running API, in aggregate only.

    python scripts/evaluate_gold.py --api http://localhost:8010 [--out data/loop/gold_totals.json]

Submits every gold PDF as one POST /documents/batch (no type hint, as a customer would), waits, then prints and stores TOTALS: documents
identified / refused / ambiguous, fields scored and right, wrong values by approval tier, auto-approved values, and the same by which reader
read the page. It never prints or stores a document name, a field name, a value or a per-field result, so a failure cannot be used to choose
a change. The labels are UNVERIFIED drafts (SPLIT.json "label_verified": false), so every figure here is a score against a draft, reported
as such. If a bug is found by other means, mark that document "burned" in SPLIT.json by hand and ask for a replacement.

Scoring is `export_batch_json.same`, imported, not copied.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from export_batch_json import same  # noqa: E402

GOLD = ROOT / "data" / "gold_real"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    split = json.loads((GOLD / "SPLIT.json").read_text(encoding="utf-8"))
    docs = [d for d in split["documents"] if not d.get("burned")]
    files = [("files", (d["file"], (GOLD / d["file"]).read_bytes(), "application/pdf")) for d in docs]
    t0 = time.time()
    r = httpx.post(args.api + "/documents/batch", files=files, timeout=600)
    r.raise_for_status()
    bid = r.json()["batch_id"]
    while True:
        with urllib.request.urlopen(f"{args.api}/documents/batch/{bid}/status", timeout=60) as resp:
            status = json.load(resp)
        if not any(d["status"] in ("queued", "processing", "retrying") for d in status["documents"]):
            break
        time.sleep(10)
    seconds = time.time() - t0

    labels = {d["file"]: json.loads((GOLD / "labels" / d["label_file"]).read_text(encoding="utf-8"))["fields"] for d in docs}
    outcome: Counter = Counter()
    fields: Counter = Counter()
    by_reader: dict[str, Counter] = {}
    for doc in status["documents"]:
        if doc["status"] != "completed":
            outcome["failed"] += 1
            continue
        page = doc["result"]["pages"][0]  # the labels are of the first page
        if not page["confirmed"]:
            outcome[f"refused:{page['reason']}"] += 1
            continue
        outcome["identified"] += 1
        c = by_reader.setdefault(page["reader"], Counter())
        for fid, want in labels[doc["filename"]].items():
            got = page["result"]["fields"].get(fid)
            if got is None:
                continue
            ok = same(fid, got["value"], want)
            tier = got["approval_tier"]
            for k in (fields, c):
                k["scored"] += 1
                k["right"] += ok
                k[f"tier_{tier}"] += 1
                if not ok:
                    k[f"wrong_{tier}"] += 1
    totals = {"labels": "UNVERIFIED drafts", "documents": len(docs), "outcomes": dict(outcome), "fields": dict(fields),
              "by_reader": {k: dict(v) for k, v in sorted(by_reader.items())}, "seconds": round(seconds), "docs_per_minute": round(len(docs) / (seconds / 60), 1)}
    print(json.dumps(totals, indent=1))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(totals, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
