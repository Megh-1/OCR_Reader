"""
Cuts the search region of every field of a waybill template out of each labelled page, for a vision-model reader. The cutting
itself is app/modules/vlm_reader/crops.py (what ships); this script feeds it the research corpus and writes the files.

  python scripts/make_crops.py            # all labelled B-form pages -> data/real_awb/work/crops + manifest.json

One crop per field, except the cells that hold several values: a party cell (name + address) and the goods cell
(description + HS code + invoice) are cut once and asked for together. A crop is the aligned region of the page
(the template box moved by landmark alignment, widened as scripts/proto_select.py does) plus a little context.
Crops are written for every document; which of them may be scored is decided by data/real_awb/split.json, not here.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402

from app.config import TESSERACT_CMD  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

from app.modules import alignment as A, pipeline, preprocessing  # noqa: E402
from app.modules.vlm_reader import crops as AC, waybill as W  # noqa: E402
from app.modules.vlm_reader.crops import REGISTER_MIN_GAIN, _text_mask, snap, text_blobs  # noqa: E402,F401  (re-exported: measurement scripts use them)
from app.modules.vlm_reader.profile import Profile  # noqa: E402
from scripts import proto_select as P  # noqa: E402

ROOT = P.ROOT
OUT = ROOT / "work" / "crops"
CONTEXT = W.CONTEXT

REFERENCE = {"B": "08_176021952812_emirates.json", "M": "09_176060677362_reference.json"}[P.FORM]  # the page the template was cut from
RULES = json.loads((ROOT / "work" / f"rules_{P.FORM}.json").read_text(encoding="utf-8"))  # the form's printed rules (template frame)
if P.FORM == "M":  # raster detection gives [x0, x1, y] / [x, y0, y1] lists already
    RULES = {"h": [tuple(r) for r in RULES["h"]], "v": [tuple(r) for r in RULES["v"]]}



LEVELS = W.LEVELS
_FORM_BLOBS: list = []
REF_PRINTED: list = []


def form_blobs() -> list[tuple[float, float, float, float]]:
    """The printed form's own text as template-frame boxes: the reference page with its typed values removed, read from the ink."""
    if not _FORM_BLOBS:
        import fitz
        pdf = next(ROOT.glob(f"*/{json.loads((ROOT / 'labels' / REFERENCE).read_text(encoding='utf-8'))['file']}"))
        doc = fitz.open(pdf)
        page = doc[0]
        W, H = page.rect.width, page.rect.height
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                for s in ln["spans"]:
                    x0, y0, x1, y1 = s["bbox"][0] / W, s["bbox"][1] / H, s["bbox"][2] / W, s["bbox"][3] / H
                    area = max(1e-9, (x1 - x0) * (y1 - y0))
                    for f in P.FIELDS.values():
                        ix, iy = max(0.0, min(x1, f.bbox[2]) - max(x0, f.bbox[0])), max(0.0, min(y1, f.bbox[3]) - max(y0, f.bbox[1]))
                        if ix * iy / area >= 0.5:
                            page.add_redact_annot(s["bbox"])
                            break
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        pix = page.get_pixmap(dpi=200)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]
        mask = cv2.dilate(_text_mask(Image.fromarray(img)), np.ones((3, 5), np.uint8))
        n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
        _FORM_BLOBS.extend((x / pix.width, y / pix.height, (x + w) / pix.width, (y + h) / pix.height) for x, y, w, h, _a in stats[1:n])
        doc.close()
    return _FORM_BLOBS


def load_reference_printed() -> None:
    """the form's own printed words: the words of the reference page that sit outside every marked value box"""
    global REF_PRINTED
    tokens_all = json.loads((ROOT / "work" / "tokens.json").read_text(encoding="utf-8"))
    ref = [tuple(t) for t in tokens_all[REFERENCE]["native"]]
    REF_PRINTED = [t for t in ref if not any(f.bbox[0] <= (t[1] + t[3]) / 2 <= f.bbox[2] and f.bbox[1] <= (t[2] + t[4]) / 2 <= f.bbox[3] for f in P.FIELDS.values())]


_PROFILE: list = []


def profile() -> Profile:
    """The research form's profile, built from the reference page the same way scripts/build_reader_profile.py stores it."""
    if not _PROFILE:
        load_reference_printed()
        _PROFILE.append(Profile(
            name=f"research_{P.FORM}", rules_h=tuple(tuple(r) for r in RULES["h"]), rules_v=tuple(tuple(r) for r in RULES["v"]),
            printed=tuple(tuple(t) for t in REF_PRINTED), form_blobs=tuple(tuple(b) for b in form_blobs())))
    return _PROFILE[0]


def crops_for_page(page: Image.Image, al: A.Alignment, out_dir: Path, doc: str, levels=LEVELS) -> list[dict]:
    """Cut every field's search region, at each widening level, from an already-aligned page and write the PNGs; returns the manifest
    entries (relative image paths). `region` is where the crop sits on the page, as page fractions."""
    d = out_dir / doc
    d.mkdir(parents=True, exist_ok=True)
    entries = []
    for crop in AC.crops_for_page(page, al, profile(), P.FIELDS, levels):
        key = crop.fields[0] if crop.kind == "single" else crop.label
        crop.image.save(d / f"{key}@{crop.level}.png")
        entry = {"doc": doc, "kind": crop.kind, "fields": crop.fields, "image": f"{doc}/{key}@{crop.level}.png", "label": crop.label,
                 "captions": crop.captions}
        if crop.kind == "single":
            entry["hint"] = crop.hint
        entries.append({**entry, "level": crop.level, "region": crop.region})
    return entries


def align_page(pdf_bytes: bytes, filename: str) -> tuple[Image.Image, A.Alignment | None]:
    """page image (second-pass grey) and its alignment, read from one page-level OCR of that image"""
    pages, meta, layers = pipeline.prepare_document(pdf_bytes, filename, None)
    page = Image.open(io.BytesIO(preprocessing.preprocess_image(pages[0], deskew=layers[0] is None).field_image_bytes))
    page.load()
    # alignment reads the page with its printed rules erased (the ruled-form second pass), as the pipeline does; the crops stay
    # cut from the page as scanned
    erased = Image.open(io.BytesIO(preprocessing.preprocess_image(pages[0], remove_rules=True, deskew=layers[0] is None).field_image_bytes))
    erased.load()
    toks = P.clean([tuple(t) for t in __import__("scripts.proto_tokens", fromlist=["x"]).page_tokens_from_ocr(erased, 3)])
    words = [A.PageWord(P.key(t[0]), t[1], (t[2] + t[4]) / 2) for t in toks if len(re.sub(r"[^A-Za-z]", "", t[0])) >= 4 and t[5] >= 30]
    return page, A.fit_alignment(P.LM, words)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    load_reference_printed()
    tokens_all = json.loads((ROOT / "work" / "tokens.json").read_text(encoding="utf-8"))
    manifest = []
    for name, rec in tokens_all.items():
        lab = json.loads((ROOT / "labels" / name).read_text(encoding="utf-8"))
        if not lab["form"].startswith(P.FORM):
            continue
        pdf = next(ROOT.glob(f"*/{lab['file']}"))
        page, al = align_page(pdf.read_bytes(), pdf.name)
        if al is None:
            print(f"{name}: no alignment, skipped")
            continue
        manifest += crops_for_page(page, al, OUT, name[:2])
        print(f"{name}: crops written", flush=True)
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print("manifest:", len(manifest), "crops")


if __name__ == "__main__":
    main()
