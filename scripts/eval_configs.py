#!/usr/bin/env python
"""
Step 4: tiers for each reader configuration on DEV, from saved reads (no model is called here).

    python scripts/eval_configs.py --primary data/eval_real/results/page_qwen3vl_dev_ABCD.json --second data/eval_real/results/page_glmocr_dev_G2.json \
        --crop data/eval_real/results/app_dev.json [--second-name page_glmocr] [--out-dir data/eval_real/results/configs] [--append docs/reader-results.md]

Configurations: each page engine alone (nothing can be auto: no second reader), primary+second page engine, primary+crop, primary+second+crop.
The crop reads are the template path's (text layer or Tesseract) from a run_eval_real.py result; they exist only for a page whose template matched.
Dev only (the page text it grounds against is the scorer's cache).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.modules.page_tiers import tier_fields  # noqa: E402
from scripts.score_real import _page_text, load_split, score, table  # noqa: E402
from scripts.seed_templates import AIR_WAYBILL  # noqa: E402


def build(primary: dict, seconds: dict[str, dict], name: str, split: str = "dev") -> dict:
    manifest, _labels, _v = load_split(split)
    docs = {}
    for entry in manifest:
        d = primary["docs"].get(entry["doc_id"])
        if not d or d.get("status") != "ok":
            docs[entry["doc_id"]] = {"status": "failed"}
            continue
        others = {}
        for sname, sres in seconds.items():
            sd = sres["docs"].get(entry["doc_id"])
            if sd and sd.get("status") == "ok":
                others[sname] = {fid: f["value"] for fid, f in sd["fields"].items()}
        tiers = tier_fields(AIR_WAYBILL, {fid: (f["value"], f.get("source_text", "")) for fid, f in d["fields"].items()}, d["reader"], others, _page_text(split, entry))
        docs[entry["doc_id"]] = {
            "status": "ok", "template_id": None,
            "fields": {fid: {"value": t.value, "tier": t.tier, "engines": t.engines, "grounded": t.grounded, "agreed": t.agreed, "reasons": t.reasons} for fid, t in tiers.items()},
        }
    return {"config": name, "docs": docs}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary", type=Path, required=True)
    ap.add_argument("--second", type=Path, required=True)
    ap.add_argument("--crop", type=Path, required=True)
    ap.add_argument("--primary-name", default="page_qwen3vl")
    ap.add_argument("--second-name", default="page_glmocr")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "data" / "eval_real" / "results" / "configs")
    ap.add_argument("--append", type=Path, default=None)
    args = ap.parse_args()
    load = lambda p: json.loads(p.read_text(encoding="utf-8"))  # noqa: E731
    primary, second, crop = load(args.primary), load(args.second), load(args.crop)
    configs = [
        (f"{args.primary_name} alone", primary, {}),
        (f"{args.second_name} alone", second, {}),
        (f"{args.primary_name} + {args.second_name}", primary, {args.second_name: second}),
        (f"{args.primary_name} + crop (template path)", primary, {"crop": crop}),
        (f"{args.primary_name} + {args.second_name} + crop", primary, {args.second_name: second, "crop": crop}),
    ]
    args.out_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    for name, prim, seconds in configs:
        res = build(prim, seconds, name)
        (args.out_dir / (name.replace(" ", "_").replace("+", "and").replace("(", "").replace(")", "") + ".json")).write_text(json.dumps(res, indent=1), encoding="utf-8")
        sc = score(res, "dev")
        chunks.append(table(name, sc))
        print(chunks[-1] + "\n")
    if args.append:
        with args.append.open("a", encoding="utf-8") as fh:
            fh.write("\n## Tiers per configuration (Step 4), DEV\n\n" + "\n\n".join(chunks) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
