#!/usr/bin/env python
"""
Groups the real air waybills by layout / printer.

For each document: page 1 is prepared as the pipeline prepares it and read once for its words with their positions
(`alignment.read_page_words`). Two documents are as similar as the share of the smaller document's words that the alignment fit
(scale + shift per axis, RANSAC) finds at consistent places on the other, i.e. the same words in the same layout. Plain word overlap does
not separate layouts: every IATA waybill shares its vocabulary (measured: Jaccard 0.27-0.81 for all pairs, one group). Groups are found by
average-linkage clustering at a similarity threshold. Nothing here is trained or stored: it only prints the
groups, with each document's difficulty tag (the folder it was filed under: Easy / Medium / Difficult) and the layout the person
who labelled it wrote in the label file.

    python scripts/cluster_real_awb.py [--root data/real_awb] [--threshold 0.40]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import TESSERACT_CMD  # noqa: E402
from app.modules import pipeline, preprocessing  # noqa: E402
from app.modules.alignment import fit_alignment, landmarks_from_words, read_page_words  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def page_words(pdf: Path):
    pages, _meta, layers = pipeline.prepare_document(pdf.read_bytes(), pdf.name, None)
    generic = preprocessing.preprocess_image(pages[0], deskew=layers[0] is None)
    image = Image.open(io.BytesIO(generic.image_bytes))
    image.load()
    return read_page_words(image)


def similarity(a, b) -> float:
    """Share of the smaller document's words that agree in position with the other's, in [0, 1] (0 when they do not align at all)."""
    cand = landmarks_from_words(a)
    fit = fit_alignment(cand, b, min_landmarks=8, min_fraction=0.0)
    return 0.0 if fit is None else fit.landmarks / max(1, min(len(a), len(b)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", type=Path, default=ROOT / "data" / "real_awb")
    ap.add_argument("--threshold", type=float, default=0.40)
    args = ap.parse_args()

    labels = {}
    for lp in sorted((args.root / "labels").glob("*.json")):
        d = json.loads(lp.read_text(encoding="utf-8"))
        labels[d["file"]] = (lp.stem, d.get("form", ""))
    docs = []
    for pdf in sorted(args.root.glob("*/*.pdf")):
        stem, form = labels.get(pdf.name, ("?", ""))
        docs.append({"id": stem.split("_")[0], "tag": pdf.parent.name, "file": pdf.name, "form": form, "words": page_words(pdf)})

    sim = {(a["id"], b["id"]): max(similarity(a["words"], b["words"]), similarity(b["words"], a["words"])) for a in docs for b in docs}
    groups = [[d] for d in docs]
    while True:  # average linkage
        best, pair = args.threshold, None
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                s = sum(sim[a["id"], b["id"]] for a in groups[i] for b in groups[j]) / (len(groups[i]) * len(groups[j]))
                if s > best:
                    best, pair = s, (i, j)
        if pair is None:
            break
        i, j = pair
        groups[i] += groups.pop(j)

    for n, g in enumerate(sorted(groups, key=lambda g: -len(g)), 1):
        tags = {t: sum(1 for d in g if d["tag"] == t) for t in sorted({d["tag"] for d in g})}
        print(f"\ngroup {n}: {len(g)} documents, difficulty {tags}")
        for d in sorted(g, key=lambda d: d["id"]):
            print(f"  {d['id']:>3} {d['tag']:<9} {d['file'][:44]:<44} labelled as: {d['form'][:70]}")
    print("\npairwise similarity (positional agreement):")
    ids = [d["id"] for d in docs]
    print("     " + " ".join(f"{i:>4}" for i in ids))
    for a in docs:
        print(f"{a['id']:>4} " + " ".join(f"{sim[a['id'], b['id']]:4.2f}" for b in docs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
