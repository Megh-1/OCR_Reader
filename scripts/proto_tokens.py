"""
Prototype (measurement only): every word on each labelled page, with its box, from ONE page-level read.

  text-native page -> the PDF's own words (exact, confidence 100)
  scan             -> Tesseract image_to_data on the whole page, run on the template's second-pass image (rules erased)

Cached to data/real_awb/work/tokens.json so the selection logic can be iterated without re-running OCR.
usage: python scripts/proto_tokens.py [--psm 3]
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import TESSERACT_CMD  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

from app.modules import pipeline, preprocessing  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "data" / "real_awb"
OUT = ROOT / "work" / "tokens.json"


def page_tokens_from_ocr(image: Image.Image, psm: int) -> list[list]:
    d = pytesseract.image_to_data(image, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(d["text"]):
        if not t.strip() or float(d["conf"][i]) < 0:
            continue
        x0, y0 = d["left"][i] / image.width, d["top"][i] / image.height
        out.append([t, x0, y0, x0 + d["width"][i] / image.width, y0 + d["height"][i] / image.height, float(d["conf"][i])])
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--psm", type=int, default=3)
    args = ap.parse_args()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result = {}
    for lab in sorted((ROOT / "labels").glob("*.json")):
        doc = json.loads(lab.read_text(encoding="utf-8"))
        pdf = next(ROOT.glob(f"*/{doc['file']}"))
        pages, meta, layers = pipeline.prepare_document(pdf.read_bytes(), pdf.name, None)
        t0 = time.perf_counter()
        generic = preprocessing.preprocess_image(pages[0], deskew=layers[0] is None)
        # second pass with the ruled-form hint: printed rules erased, so they are not read as characters
        pass2 = preprocessing.preprocess_image(pages[0], remove_rules=True, deskew=layers[0] is None)
        image = Image.open(io.BytesIO(pass2.field_image_bytes))
        image.load()
        ocr = page_tokens_from_ocr(image, args.psm)
        native = None
        if layers[0] is not None:
            native = [[w.text, *w.bbox, 100.0] for w in layers[0].words]
        result[lab.name] = {"file": doc["file"], "native": native, "ocr": ocr, "size": list(image.size)}
        print(f"{lab.name:42s} {'native' if native else 'scan  '} ocr words {len(ocr):4d}  {time.perf_counter() - t0:.1f}s", flush=True)
    OUT.write_text(json.dumps(result), encoding="utf-8")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
