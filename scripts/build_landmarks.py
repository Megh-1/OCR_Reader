"""
Builds a template's page-alignment landmarks (app/modules/alignment.py) from real documents.

A landmark is a printed word at a fixed place on the form. Words that are DATA on the reference
page (names, cities, numbers) must not become landmarks, so the rule is: a reference word is kept
only if it turns up at a consistent position on at least --min-docs OTHER documents of the same
layout. Printed form text recurs across documents; one shipper's name does not. That also keeps
the committed file free of anyone's details.

Each other document is first aligned to the reference using ALL the reference's words (the
alignment's own outlier rejection ignores the data words), then every reference word it matched is
counted.

    python scripts/build_landmarks.py --reference REF.pdf --others A.pdf B.pdf ... \
        --out scripts/landmarks/air_waybill.json [--min-docs 3] [--min-landmarks 8]

Only page 1 of each file is used. Documents that do not align to the reference (a different layout)
contribute nothing, and are listed so that is visible.
"""

import argparse
import io
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import TESSERACT_CMD  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

from app.modules import pipeline, preprocessing  # noqa: E402
from app.modules.alignment import fit_alignment, landmarks_from_words, read_page_words  # noqa: E402


def page_words(path: Path):
    pages, _meta, layers = pipeline.prepare_document(path.read_bytes(), path.name, None)
    generic = preprocessing.preprocess_image(pages[0], deskew=layers[0] is None)
    image = Image.open(io.BytesIO(generic.image_bytes))
    image.load()
    return read_page_words(image)


def build(reference: Path, others: list[Path], min_docs: int, min_landmarks: int) -> tuple[list[dict], list[str]]:
    candidates = landmarks_from_words(page_words(reference))
    counts: Counter = Counter()
    notes = []
    for path in others:
        alignment = fit_alignment(candidates, page_words(path), min_landmarks=min_landmarks, min_fraction=0.0)
        if alignment is None:
            notes.append(f"{path.name}: does not align to the reference (different layout) - not counted")
            continue
        counts.update(alignment.matched)
        notes.append(f"{path.name}: aligned on {alignment.landmarks} words (rms {alignment.rms * 100:.2f}% of the page)")
    kept = [
        {"text": c.text, "x": round(c.x, 4), "y": round(c.y, 4)}
        for i, c in enumerate(candidates)
        if counts[i] >= min_docs
    ]
    return kept, notes + [f"{len(candidates)} words on the reference page -> {len(kept)} recur on >= {min_docs} other documents"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--reference", required=True, type=Path)
    ap.add_argument("--others", required=True, nargs="+", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--min-docs", type=int, default=3)
    ap.add_argument("--min-landmarks", type=int, default=8, help="fewest words that must agree for a document to count while building")
    args = ap.parse_args()

    kept, notes = build(args.reference, args.others, args.min_docs, args.min_landmarks)
    for line in notes:
        print(line)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(kept, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {len(kept)} landmarks to {args.out}")


if __name__ == "__main__":
    main()
