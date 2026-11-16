#!/usr/bin/env python
"""
Build the real-document evaluation sets (sprint Step 2). Writes under data/eval_real/ (git-ignored):

    dev/   real waybills 01 02 06 08 09 10 11 12   - the ONLY set anything may be tuned on
    test/  real waybills 03 04 05 07 13 14 15 16 + anything under data/new_real/   - never opened, printed or tuned on

Per document: <doc_id>.pdf (or the image) + <doc_id>.png (page 1, 200 DPI) and, per split, labels.csv (doc_id, field_id, value, verified_by) for
the 33 air_waybill field ids, prefilled from the draft labels in data/real_awb/labels/ ('' when none), plus manifest.json (doc_id, file, kind).
`verified_by` is left EMPTY: only a person who read the image fills it in. This script prints counts only, never a value, and never a test row.

    python scripts/build_eval_real.py
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
from PIL import Image  # noqa: E402

from app.modules.ingestion import extract_text_layers  # noqa: E402
from scripts.seed_templates import AIR_WAYBILL  # noqa: E402

DEV_IDS = ["01", "02", "06", "08", "09", "10", "11", "12"]
TEST_IDS = ["03", "04", "05", "07", "13", "14", "15", "16"]
OUT = ROOT / "data" / "eval_real"
FIELD_IDS = [f.id for f in AIR_WAYBILL.fields]
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def _label_files() -> dict[str, Path]:
    return {p.name.split("_")[0]: p for p in (ROOT / "data" / "real_awb" / "labels").glob("*.json") if p.name[0].isdigit()}


def _find_pdf(name: str) -> Path:
    hits = [p for p in (ROOT / "data" / "real_awb").glob("*/*.pdf") if p.name == name]
    if not hits:
        raise SystemExit(f"source file for a labelled document is missing: {name}")
    return hits[0]


def _page_png(path: Path) -> tuple[bytes, str]:
    """Page-1 PNG at 200 DPI and the input kind: digital (real text layer), scan (PDF without one) or photo (an image file)."""
    if path.suffix.lower() == ".pdf":
        data = path.read_bytes()
        layers = extract_text_layers(data)
        kind = "digital" if layers and layers[0] is not None else "scan"
        pix = fitz.open(stream=data, filetype="pdf")[0].get_pixmap(dpi=200)
        return pix.tobytes("png"), kind
    img = Image.open(path).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), "photo"


def _build(split: str, entries: list[tuple[str, Path, dict]]) -> int:
    d = OUT / split
    d.mkdir(parents=True, exist_ok=True)
    manifest, rows = [], []
    for doc_id, src, values in entries:
        png, kind = _page_png(src)
        (d / f"{doc_id}.png").write_bytes(png)
        shutil.copyfile(src, d / f"{doc_id}{src.suffix.lower()}")
        manifest.append({"doc_id": doc_id, "file": f"{doc_id}{src.suffix.lower()}", "kind": kind})
        rows += [(doc_id, fid, values.get(fid, ""), "") for fid in FIELD_IDS]
    with (d / "labels.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["doc_id", "field_id", "value", "verified_by"])
        w.writerows(rows)
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    kinds = {k: sum(m["kind"] == k for m in manifest) for k in ("digital", "scan", "photo")}
    print(f"{split}: {len(manifest)} documents {kinds}, {len(rows)} label rows")
    return len(manifest)


def main() -> int:
    labels = _label_files()
    split_entries: dict[str, list] = {"dev": [], "test": []}
    for split, ids in (("dev", DEV_IDS), ("test", TEST_IDS)):
        for doc_id in ids:
            meta = json.loads(labels[doc_id].read_text(encoding="utf-8"))
            split_entries[split].append((doc_id, _find_pdf(meta["file"]), meta["fields"]))
    new_dir = ROOT / "data" / "new_real"
    if new_dir.is_dir():
        for i, p in enumerate(sorted(q for q in new_dir.iterdir() if q.suffix.lower() == ".pdf" or q.suffix.lower() in IMAGE_SUFFIXES), 1):
            split_entries["test"].append((f"new{i:02d}", p, {}))
    for split in ("dev", "test"):
        _build(split, split_entries[split])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
