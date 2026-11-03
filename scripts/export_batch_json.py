#!/usr/bin/env python
"""
Turns a finished batch into ONE JSON file of what was extracted, and (with a ground truth) scores it.

    python scripts/export_batch_json.py --api http://localhost:8000 --batch-id ID \\
        --ground-truth data/demo_50/ground_truth.json --manifest data/demo_50/manifest.json --out data/demo_50_results

Writes OUT/extracted.json - per file: the template and reader that read it, the document's status and, per field, the value, the
approval tier (auto / spot_check / review), the confidence and why a field was flagged - and OUT/scorecard.json: how many fields
were right overall, by input kind and by tier, and above all how many WRONG values sat in each tier. A wrong value in `auto` is
the number that matters; a wrong one in `spot_check` or `review` was caught by design.

The score compares a value with the generator's ground truth after collapsing whitespace and ignoring case, so it measures what was
read, not how the value was spaced.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path


def norm(value) -> str:
    return " ".join(str(value).strip().lower().split())


def fetch(api: str, batch_id: str) -> dict:
    with urllib.request.urlopen(f"{api}/documents/batch/{batch_id}/status", timeout=60) as response:
        return json.load(response)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--batch-id", required=True)
    ap.add_argument("--ground-truth", type=Path)
    ap.add_argument("--manifest", type=Path, help="manifest.json from build_demo_batch.py: the input kind of each file")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--wait", action="store_true", help="poll until every document has finished")
    args = ap.parse_args()

    status = fetch(args.api, args.batch_id)
    while args.wait and any(d["status"] in ("queued", "processing", "retrying") for d in status["documents"]):
        done = sum(d["status"] in ("completed", "failed") for d in status["documents"])
        print(f"\r{done}/{len(status['documents'])} finished", end="", flush=True)
        time.sleep(15)
        status = fetch(args.api, args.batch_id)
    print()

    args.out.mkdir(parents=True, exist_ok=True)
    truth = json.loads(args.ground_truth.read_text(encoding="utf-8")) if args.ground_truth else {}
    kinds = json.loads(args.manifest.read_text(encoding="utf-8"))["kinds"] if args.manifest else {}

    extracted: dict[str, dict] = {}
    right = Counter(); total = Counter()
    wrong_by_tier: Counter = Counter(); fields_by_tier: Counter = Counter()
    by_kind: dict[str, Counter] = defaultdict(Counter)
    doc_outcomes: Counter = Counter()
    for doc in sorted(status["documents"], key=lambda d: d["filename"]):
        name = doc["filename"]
        if doc["status"] != "completed":
            extracted[name] = {"status": doc["status"], "error": doc.get("error")}
            doc_outcomes[doc["status"]] += 1
            continue
        pages = doc["result"]["pages"]
        entry = {"status": "completed", "kind": kinds.get(name), "pages": []}
        for page in pages:
            if not page["confirmed"]:
                entry["pages"].append({"confirmed": False, "reason": page["reason"]})
                doc_outcomes[f"refused:{page['reason']}"] += 1
                continue
            result = page["result"]
            fields = {fid: {"value": f["value"], "approval_tier": f["approval_tier"], "confidence": f["confidence"], "reasons": f["reasons"]}
                      for fid, f in result["fields"].items()}
            entry["pages"].append({"confirmed": True, "template_id": result["template_id"], "template_version": result["version"],
                                   "reader": page["reader"], "overall_status": result["overall_status"], "fields": fields})
            doc_outcomes[f"{result['template_id']}:{page['reader']}"] += 1
            expected = truth.get(name)
            if expected is None:
                continue
            if expected["template_id"] != result["template_id"]:
                doc_outcomes["wrong_template"] += 1
                continue
            for fid, want in expected["fields"].items():
                got = fields.get(fid)
                if got is None:
                    continue
                tier = got["approval_tier"]
                ok = norm(got["value"]) == norm(want)
                total["fields"] += 1; right["fields"] += ok
                fields_by_tier[tier] += 1
                kind = kinds.get(name, "?")
                by_kind[kind]["fields"] += 1; by_kind[kind]["right"] += ok
                if not ok:
                    wrong_by_tier[tier] += 1
                    by_kind[kind][f"wrong_{tier}"] += 1
        extracted[name] = entry

    (args.out / "extracted.json").write_text(json.dumps(extracted, indent=1), encoding="utf-8")
    scorecard = {
        "batch_id": args.batch_id,
        "documents": len(status["documents"]),
        "document_outcomes": dict(doc_outcomes),
        "fields_scored": total["fields"],
        "fields_right": right["fields"],
        "fields_by_tier": dict(fields_by_tier),
        "wrong_by_tier": dict(wrong_by_tier),
        "by_input_kind": {k: dict(v) for k, v in sorted(by_kind.items())},
    }
    (args.out / "scorecard.json").write_text(json.dumps(scorecard, indent=1), encoding="utf-8")
    print(json.dumps(scorecard, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
