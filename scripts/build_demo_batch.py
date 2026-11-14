#!/usr/bin/env python
"""
Builds a hard, labelled demo batch of 50 DIFFERENT fake air waybills (Faker values, exact ground truth) to upload at once.

Two forms, 25 documents each: the Mumbai-printer form (`air_waybill`, from the real waybill in tests/fixtures) and the Bangkok
form (`air_waybill_bkk`, from real_awb #8). Each document is drawn once and then handed in as ONE of its variants - a digital PDF
with a text layer, a scanned PDF, or a camera photo (M only) - so no two files share values. The generators are run with --hard
(worse print misregistration and scanner), --varied (real-style formats, other fonts, values that overflow their cell) and, for M,
--drift (the typed layer shifted against the printed form, zone by zone) and --inject-errors (internally inconsistent documents the
cross-field checks must catch; they are always in the batch).

What it is not: another airline's form. Same two layouts the model was trained on, new seeds and new Faker values, so it measures
content and scan quality on known forms, not unseen layouts.

    python scripts/build_demo_batch.py --out data/demo_50 [--seed 7001]

`--difficulty hardest` builds a different mix for a SEALED hold-out set: 26 Mumbai-form documents (8 injected-inconsistent digital
files and 18 camera photos, the only form that has photos) and 24 Bangkok-form documents (10 digital, 14 scanned), i.e. 18 digital /
14 scanned / 18 photos with 8 injected inconsistencies. The generators and their flags are unchanged. The default (`standard`) path
makes the same documents it always did: the PDF bytes differ run to run (container timestamps) but the ground truth, the manifest
and every page's pixels and text do not, and that was checked before and after this option was added.
"""

from __future__ import annotations

import argparse
import json
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def run(*args) -> None:
    subprocess.run([PY, *map(str, args)], check=True, cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT)})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "demo_50")
    ap.add_argument("--seed", type=int, default=7001)
    ap.add_argument("--per-form", type=int, default=None, help="documents per form (standard: 25)")
    ap.add_argument("--inject", type=int, default=None, help="injected-inconsistent documents (standard: 5, hardest: 8)")
    ap.add_argument("--difficulty", choices=["standard", "hardest"], default="standard")
    args = ap.parse_args()
    hardest = args.difficulty == "hardest"
    n_m = args.per_form or (26 if hardest else 25)
    n_b = args.per_form or (24 if hardest else 25)
    args.inject = args.inject if args.inject is not None else (8 if hardest else 5)
    base_m = next((ROOT / "tests" / "fixtures").glob("17606067736*.pdf"))
    rng = random.Random(args.seed)
    if args.out.exists():
        shutil.rmtree(args.out)
    args.out.mkdir(parents=True)

    with tempfile.TemporaryDirectory() as tmp:
        m_dir, b_dir = Path(tmp) / "m", Path(tmp) / "b"
        run("scripts/generate_synthetic_awb.py", "--base", base_m, "--out", m_dir, "--count", n_m, "--scans", n_m, "--photos", n_m,
            "--inject-errors", args.inject, "--seed", args.seed, "--hard", "--varied", "--drift")
        run("scripts/synth_bkk.py", "--out", b_dir, "--count", n_b, "--seed", args.seed + 1, "--hard", "--varied")
        truth_m = json.loads((m_dir / "ground_truth.json").read_text(encoding="utf-8"))
        truth_b = json.loads((b_dir / "ground_truth.json").read_text(encoding="utf-8"))
        injections = json.loads((m_dir / "injections.json").read_text(encoding="utf-8")) if (m_dir / "injections.json").exists() else {}
        injected = {k.split(".")[0] for k in injections}

        truth, kinds, picked_injections = {}, {}, {}
        for i in range(n_m):  # M: digital / scan / photo, injected documents stay the digital file they were injected in
            stem = f"awb_{i:04d}"
            variant = "" if stem in injected else ("__photo" if hardest else rng.choice(["", "__scan", "__photo"]))
            src = next(m_dir.glob(f"{stem}{variant}.*"))
            dst = f"demo_m_{i:02d}{variant or '__digital'}{src.suffix}"
            shutil.copy(src, args.out / dst)
            truth[dst] = truth_m[src.name]
            kinds[dst] = (variant or "__digital").strip("_")
            if stem in injected:
                picked_injections[dst] = injections[f"{stem}.pdf"]
        b_plan = rng.sample([""] * 10 + ["__scan"] * 14, n_b) if hardest else []
        for i in range(n_b):  # B: digital or scan
            stem = f"bkk_{i:04d}"
            variant = b_plan[i] if hardest else rng.choice(["", "__scan"])
            src = b_dir / f"{stem}{variant}.pdf"
            dst = f"demo_b_{i:02d}{variant or '__digital'}.pdf"
            shutil.copy(src, args.out / dst)
            truth[dst] = truth_b[src.name]
            kinds[dst] = (variant or "__digital").strip("_")

    for entry in truth.values():  # the boxes/drift bookkeeping is the generators' own; the batch needs only the values
        entry.pop("boxes", None)
    names = sorted(truth)
    (args.out / "ground_truth.json").write_text(json.dumps({k: truth[k] for k in names}, indent=1), encoding="utf-8")
    (args.out / "manifest.json").write_text(json.dumps({"kinds": {k: kinds[k] for k in names}, "injections": picked_injections, "seed": args.seed}, indent=1), encoding="utf-8")
    counts = {k: sum(1 for v in kinds.values() if v == k) for k in sorted(set(kinds.values()))}
    print(f"wrote {len(names)} documents to {args.out}: {counts}, {len(picked_injections)} injected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
