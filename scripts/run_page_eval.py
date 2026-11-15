#!/usr/bin/env python
"""
Run one page engine (app/modules/page_reader.py) on the DEV split of real waybills and write a results file for score_real.py.

    python scripts/run_page_eval.py --engine page_qwen3vl --out data/eval_real/results/page_qwen3vl_dev.json [--variant NAME]

Tiers are not decided here (that is Step 4): every non-empty value is written as `spot_check`, so the auto columns of the scorer read 0.
Only dev can be run; the test split belongs to scripts/run_final_test.py. Engines come from config/reader_engines.json (READER_ENGINES_FILE).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.modules import page_reader  # noqa: E402
from app.modules.ingestion import extract_text_layers  # noqa: E402
from app.modules.vlm_reader.engines import page_specs  # noqa: E402

EVAL = ROOT / "data" / "eval_real"


def run_one(spec, doc: dict, split_dir: Path, **kwargs) -> tuple[str, dict]:
    png = (split_dir / f"{doc['doc_id']}.png").read_bytes()
    if kwargs.pop("use_layer", False) and doc["file"].endswith(".pdf"):
        layers = extract_text_layers((split_dir / doc["file"]).read_bytes())
        if layers and layers[0] is not None:
            kwargs["layer_text"] = " ".join(w.text for w in layers[0].words)
    pr = page_reader.read_page(png, spec, **kwargs)
    return doc["doc_id"], {
        "status": "ok" if pr.ok else "failed", "template_id": None, "reader": spec.name, "seconds": pr.seconds, "image_size": list(pr.image_size),
        "reason": pr.reason,
        "fields": {fid: {"value": rf.value, "source_text": rf.source_text, "tier": "spot_check"} for fid, rf in pr.fields.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", required=True)
    ap.add_argument("--split", default="dev")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-long-edge", type=int, default=page_reader.PAGE_MAX_LONG_EDGE_PX)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--variant", default="base")
    ap.add_argument("--normalise", action="store_true", help="variant A: deterministic per-type repairs")
    ap.add_argument("--sharper", action="store_true", help="variant B: narrower wording for destination_code / flight_date")
    ap.add_argument("--halves", action="store_true", help="variant C: also read the upper and lower part at full resolution")
    ap.add_argument("--layer", action="store_true", help="variant D: give a digital page's embedded text as extra help")
    ap.add_argument("--skeleton", action="store_true", help="GLM-OCR style: the field list as a JSON skeleton, no schema mode")
    args = ap.parse_args()
    if args.split != "dev":
        raise SystemExit("only dev may be run here; test belongs to scripts/run_final_test.py")
    os.environ.setdefault("READER_ENGINES_FILE", str(ROOT / "config" / "reader_engines.json"))
    from app import config
    config.READER_ENGINES_FILE = os.environ["READER_ENGINES_FILE"]
    spec = next(s for s in page_specs() if s.name == args.engine)
    split_dir = EVAL / args.split
    manifest = json.loads((split_dir / "manifest.json").read_text(encoding="utf-8"))
    t0 = time.time()
    with ThreadPoolExecutor(args.workers) as pool:
        docs = dict(pool.map(lambda d: run_one(spec, d, split_dir, max_long_edge=args.max_long_edge, normalise=args.normalise, sharper=args.sharper, halves=args.halves, use_layer=args.layer, skeleton=args.skeleton), manifest))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"config": f"{args.engine} ({args.variant})", "seconds": round(time.time() - t0, 1), "docs": docs}, indent=1), encoding="utf-8")
    secs = sorted(d["seconds"] for d in docs.values())
    print(f"{len(docs)} documents in {time.time() - t0:.0f} s (per page {secs[0]}-{secs[-1]} s), failed {sum(d['status'] != 'ok' for d in docs.values())} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
