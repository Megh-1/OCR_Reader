#!/usr/bin/env python
"""
The final test: the ONLY runner of data/eval_real/test/, once.

    python scripts/run_final_test.py

It runs only if data/eval_real/test/LABELS_VERIFIED exists (a person has checked every test label against the image) and data/eval_real/test/SPENT
does not. Otherwise it prints why and exits without opening a test file. When it runs, each configuration gets its own API and two workers (own port,
own Redis database, settings set through the environment), the test split goes through POST /documents/batch once per DISTINCT configuration:

  * the current app configuration (the settings of the environment this script is started from; the same run is reused for the default if they match),
  * the default configuration (app/config.py defaults with config/reader_engines.json),
  * each page engine alone (page_qwen3vl, page_glmocr), no second reader.

Only totals go into docs/reader-results.md (scripts/score_real.table: documents, fields, right, auto, wrong at auto, hallucinations by input kind),
never a per-document or per-field line. Raw results go to data/eval_real/results/ (git-ignored). On success it writes test/SPENT with the date and
the commit, so a second run is refused. Nothing here is a tuning loop: the numbers are reported as they come.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TEST = ROOT / "data" / "eval_real" / "test"
RESULTS = ROOT / "data" / "eval_real" / "results"
REPORT = ROOT / "docs" / "reader-results.md"
ENGINES_FILE = "config/reader_engines.json"
DEFAULTS = {"READER_MODE": "page_first", "READER_PAGE_ENGINES": "page_qwen3vl,page_glmocr", "READER_CROP_ENGINE": "template_path", "READER_PAGE_HALVES": "1"}
SETTING_KEYS = (*DEFAULTS, "READER_ENGINES_FILE")
WORKERS = 2


def configurations() -> list[tuple[str, dict[str, str]]]:
    """(name, settings) for each run. Settings that match an earlier run are not run again: the test split is read once per distinct configuration."""
    current = {k: os.environ.get(k, DEFAULTS.get(k, ENGINES_FILE if k == "READER_ENGINES_FILE" else "")) for k in SETTING_KEYS}
    default = {**DEFAULTS, "READER_ENGINES_FILE": ENGINES_FILE}
    alone = lambda engine: {**default, "READER_PAGE_ENGINES": engine}  # noqa: E731
    return [("current app configuration", current), ("default configuration", default),
            ("page_qwen3vl alone", alone("page_qwen3vl")), ("page_glmocr alone", alone("page_glmocr"))]


def commit() -> str:
    return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip() or "unknown"


def wait_for(url: str, seconds: int = 90) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=3).read()
            return
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(1.5)
    raise RuntimeError(f"{url} did not come up")


def run_one(name: str, settings: dict[str, str], index: int) -> dict:
    from run_eval_real import batch_status, normalise, post_batch

    port = 8101 + index
    redis_base = os.environ.get("REDIS_URL", "redis://localhost:6380/0").rsplit("/", 1)[0]
    env = {**os.environ, **settings, "REDIS_URL": f"{redis_base}/{index + 1}"}
    procs = [subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=ROOT, env=env)]
    procs += [subprocess.Popen([sys.executable, "-m", "app.worker"], cwd=ROOT, env=env) for _ in range(WORKERS)]
    try:
        api = f"http://127.0.0.1:{port}"
        wait_for(f"{api}/results?limit=1")
        manifest = json.loads((TEST / "manifest.json").read_text(encoding="utf-8"))
        t0 = time.time()
        batch_id = post_batch(api, [TEST / m["file"] for m in manifest])
        while True:
            st = batch_status(api, batch_id)
            if all(d["status"] in ("completed", "failed") for d in st["documents"]):
                break
            time.sleep(3)
        return {"config": name, "settings": settings, "seconds": round(time.time() - t0, 1), "docs": normalise(st)}
    finally:
        for p in procs:
            p.terminate()


def main() -> int:
    if not (TEST / "LABELS_VERIFIED").exists():
        print("Step 7 waiting for verified labels: data/eval_real/test/LABELS_VERIFIED does not exist. Nothing was run.")
        return 1
    if (TEST / "SPENT").exists():
        print("data/eval_real/test is already spent (see test/SPENT). Nothing was run.")
        return 1

    from score_real import score, table

    RESULTS.mkdir(parents=True, exist_ok=True)
    runs: dict[str, dict] = {}  # settings key -> raw results
    sections: list[str] = []
    for i, (name, settings) in enumerate(configurations()):
        key = json.dumps(settings, sort_keys=True)
        if key in runs:
            results = {**runs[key], "config": name}
            note = " (same settings as an earlier row: the same run, not repeated)"
        else:
            results = run_one(name, settings, i)
            runs[key] = results
            (RESULTS / f"final_{i}.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
            note = ""
        sections.append(f"### {name} (test){note}\n\n{table(name, score(results, 'test', _final_test=True))}\n")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with REPORT.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## Final test (Step 7), run once on {stamp} at commit {commit()}\n\nTotals only; labels verified (test/LABELS_VERIFIED).\n\n" + "\n".join(sections))
    (TEST / "SPENT").write_text(f"{stamp} {commit()}\n", encoding="utf-8")
    print("final test done; totals appended to docs/reader-results.md; test/SPENT written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
