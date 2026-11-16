#!/usr/bin/env python
"""
Run the CURRENT app (the template pipeline, no type hint) on the dev split through POST /documents/batch and write a results file for
score_real.py.

    python scripts/run_eval_real.py --api http://localhost:8010 --split dev --out data/eval_real/results/app_dev.json [--config NAME]

Page 1 only is scored (a multi-page file's later pages are ignored). The test split is refused here: scripts/run_final_test.py is its only runner.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL = ROOT / "data" / "eval_real"


def post_batch(api: str, files: list[Path]) -> str:
    boundary = uuid.uuid4().hex
    body = b""
    for p in files:
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{p.name}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n").encode() + p.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{api}/documents/batch", data=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())["batch_id"]


def batch_status(api: str, batch_id: str) -> dict:
    return json.loads(urllib.request.urlopen(f"{api}/documents/batch/{batch_id}/status", timeout=60).read())


def normalise(status: dict) -> dict:
    docs = {}
    for d in status["documents"]:
        doc_id = Path(d["filename"]).stem
        if d["status"] != "completed":
            docs[doc_id] = {"status": "failed", "error": d.get("error")}
            continue
        page = d["result"]["pages"][0]
        if not page.get("confirmed"):
            docs[doc_id] = {"status": "refused", "reason": page.get("reason")}
            continue
        r = page["result"]
        docs[doc_id] = {
            "status": "ok", "template_id": r["template_id"], "reader": page.get("reader"),
            "fields": {fid: {"value": f["value"], "tier": f["approval_tier"], "confidence": f["confidence"]} for fid, f in r["fields"].items()},
        }
    return docs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8010")
    ap.add_argument("--split", default="dev")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--config", default="app")
    args = ap.parse_args()
    if args.split != "dev":
        raise SystemExit("only dev may be run here; test belongs to scripts/run_final_test.py")
    manifest = json.loads((EVAL / args.split / "manifest.json").read_text(encoding="utf-8"))
    files = [EVAL / args.split / m["file"] for m in manifest]
    t0 = time.time()
    batch_id = post_batch(args.api, files)
    while True:
        st = batch_status(args.api, batch_id)
        if all(d["status"] in ("completed", "failed") for d in st["documents"]):
            break
        time.sleep(2)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"config": args.config, "seconds": round(time.time() - t0, 1), "docs": normalise(st)}, indent=1), encoding="utf-8")
    print(f"{len(st['documents'])} documents in {time.time() - t0:.0f} s -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
