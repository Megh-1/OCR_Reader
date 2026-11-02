"""
Finds the long printed rules of a ruled form in a page image: horizontal rules as (x0, x1, y) and vertical ones as (x, y0, y1),
page fractions - the same shape scripts/make_crops.py reads for its cell geometry.

  python scripts/detect_rules.py data/real_awb/Easy/17606067736....pdf data/real_awb/work/rules_M.json

A form whose PDF carries its rules as vector drawings could read them exactly; most scans and raster forms cannot, so this
detects them from the rendered page (a morphological opening with a long line, the method that matched 22 of 22 rules on five
real scans).
"""

import io
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules import pipeline, preprocessing  # noqa: E402

MIN_H = 0.12  # a horizontal rule at least this fraction of the page wide
MIN_V = 0.04  # a vertical rule at least this fraction of the page tall


def detect(gray: np.ndarray) -> dict:
    h, w = gray.shape
    ink = (gray < 128).astype(np.uint8) * 255
    hm = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (max(30, int(MIN_H * w)), 1)))
    vm = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(30, int(MIN_V * h)))))
    hs, vs = [], []
    rows = (hm > 0).sum(axis=1)
    y = 0
    while y < h:
        if rows[y] > 0:
            y0 = y
            while y < h and rows[y] > 0:
                y += 1
            cols = np.where((hm[y0:y] > 0).any(axis=0))[0]
            hs.append([round(float(cols.min() / w), 4), round(float(cols.max() / w), 4), round(float((y0 + y - 1) / 2 / h), 4)])
        y += 1
    cols_ = (vm > 0).sum(axis=0)
    x = 0
    while x < w:
        if cols_[x] > 0:
            x0 = x
            while x < w and cols_[x] > 0:
                x += 1
            rws = np.where((vm[:, x0:x] > 0).any(axis=1))[0]
            vs.append([round(float((x0 + x - 1) / 2 / w), 4), round(float(rws.min() / h), 4), round(float(rws.max() / h), 4)])
        x += 1
    return {"h": hs, "v": vs}


def main() -> None:
    pdf, out = Path(sys.argv[1]), Path(sys.argv[2])
    pages, meta, layers = pipeline.prepare_document(pdf.read_bytes(), pdf.name, None)
    gray = np.array(Image.open(io.BytesIO(preprocessing.preprocess_image(pages[0], deskew=layers[0] is None).image_bytes)).convert("L"))
    rules = detect(gray)
    out.write_text(json.dumps(rules), encoding="utf-8")
    print(f"{len(rules['h'])} horizontal and {len(rules['v'])} vertical rules -> {out}")


if __name__ == "__main__":
    main()
