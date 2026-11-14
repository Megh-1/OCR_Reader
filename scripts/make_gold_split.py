#!/usr/bin/env python
"""
Sets aside the GOLD real waybills: documents that no template, landmark list, generator, format pool or tuning step may be built or chosen
from, and that scoring reads in aggregate only (scripts/evaluate_gold.py prints totals, never a per-field line).

    python scripts/make_gold_split.py            # copies the chosen files to data/gold_real/, writes SPLIT.json, marks them read-only

Refuses to run if data/gold_real already exists (the split is made once; a burned document is marked in SPLIT.json, not re-drawn silently).
The choice below was made from scripts/cluster_real_awb.py's layout groups and the people-made difficulty tag (the folder), with these
aims, in order: (1) nothing the Tesseract pipeline's generators were written from (split.json "train": 02 06 08 11 12) is gold, so the
synthetic format pool and the template bases stay in the dev pool; (2) about half the gold documents belong to a layout that ALSO has
documents left in the dev pool, and about half to a layout with NO document in it; (3) every difficulty. There are only 16 real
documents, so at most 8 can be gold while leaving a dev pool; the layouts and tags are in SPLIT.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REAL = ROOT / "data" / "real_awb"
GOLD = ROOT / "data" / "gold_real"

# id -> (layout group from cluster_real_awb.py, which side of the aim (2) it is on)
CHOSEN = {
    "03": ("bangkok-print scans", "layout stays in dev"),
    "13": ("bangkok-print scans", "layout stays in dev"),
    "14": ("bangkok-print scans", "layout stays in dev"),
    "07": ("mumbai-print text-native", "layout stays in dev"),
    "04": ("pioneer own house form", "layout absent from dev"),
    "05": ("indigo print of the bangkok form", "layout absent from dev"),
    "15": ("salamair", "layout absent from dev"),
    "16": ("salamair", "layout absent from dev"),
}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if GOLD.exists():
        print(f"{GOLD} already exists: the split is made once", file=sys.stderr)
        return 1
    labels = {p.stem.split("_")[0]: p for p in sorted((REAL / "labels").glob("*.json"))}
    pdfs = {p.name: p for p in REAL.glob("*/*.pdf")}
    (GOLD / "labels").mkdir(parents=True)
    docs, taken = [], set()
    for doc_id, (layout, side) in sorted(CHOSEN.items()):
        lab = json.loads(labels[doc_id].read_text(encoding="utf-8"))
        src = pdfs[lab["file"]]
        shutil.copy(src, GOLD / src.name)
        shutil.copy(labels[doc_id], GOLD / "labels" / labels[doc_id].name)
        taken.add(doc_id)
        docs.append({"id": doc_id, "file": src.name, "difficulty_tag": src.parent.name, "layout": layout, "side": side, "sha256": sha(GOLD / src.name),
                     "label_file": labels[doc_id].name, "label_sha256": sha(GOLD / "labels" / labels[doc_id].name), "label_verified": False,
                     "label_note": "an unverified draft; a score against it is reported separately and is not a measure of correctness until a second person signs off",
                     "burned": False})
    dev = [{"id": i, "file": json.loads(p.read_text(encoding="utf-8"))["file"]} for i, p in sorted(labels.items()) if i not in taken]
    split = {
        "made": date.today().isoformat(),
        "rule": "gold documents and their labels are never an input to a template, landmark list, generator, format pool, threshold or any tuning step; scoring reads them in aggregate only",
        "documents": docs,
        "dev_pool": dev,
        "exposure_disclosed": [
            "03 13 14 07 (and 01) were the held-out pages of the VISION reader's experiment and were read per field then (error analysis, 2026-11); no Tesseract-path asset was derived from them",
            "04 and 15 were TRAIN pages of the vision adapter (separate GPU-side training, not used by the Tesseract pipeline)",
            "scripts/landmarks/*_trainonly.json belong to the vision reader's frozen replay test and are not used by the pipeline; air_waybill.json and air_waybill_bkk.json are rebuilt from the dev pool only (PROVENANCE.json)",
            "the hard-coded formats in scripts/synth_formats.py were written from the TRAIN pages 02 06 08 11 12 only, none of which is gold",
        ],
        "counts": {"gold": len(docs), "dev": len(dev), "real_total": len(docs) + len(dev)},
    }
    (GOLD / "SPLIT.json").write_text(json.dumps(split, indent=1) + "\n", encoding="utf-8")
    for p in [*GOLD.rglob("*")]:
        if p.is_file():
            os.chmod(p, stat.S_IREAD)
    print(f"gold: {len(docs)} documents ({sum(1 for d in docs if d['side'].startswith('layout stays'))} in a layout kept in the dev pool, "
          f"{sum(1 for d in docs if d['side'].startswith('layout absent'))} in a layout absent from it); dev pool: {len(dev)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
