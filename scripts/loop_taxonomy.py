#!/usr/bin/env python
"""
Gives every field that is WRONG or CORRECT-BUT-NOT-AUTO of a finished batch exactly one cause. For the Tesseract-only measurement loop.

    PYTHONPATH=. python scripts/loop_taxonomy.py --docs-dir data/demo_50 --ground-truth data/demo_50/ground_truth.json \
        --manifest data/demo_50/manifest.json --extracted data/loop/baseline_dev/extracted.json > data/loop/baseline_dev/taxonomy.json

Needs the database with the seeded templates (run it inside the app container: the batch's own values, tiers and reasons come from
extracted.json, i.e. from the API path; the geometry is recomputed here with the same pipeline functions, so the window a field was read in is
the window used). Causes, first match wins:

  ID    the document was refused or ambiguous at Module 5 (no field was read)
  PRE   the page itself is unreadable to the page-wide OCR (fewer than 30% of the document's true value words are found anywhere on it)
  VAL   correct, but the field has no regex and no checksum, so it can never be `auto`
  GATE  correct, has a validator, and still not `auto` (confidence under the line, or a cross-check / floor rejected a right value)
  FMT   same characters, different format ('635.0' for '635.00', separators, case, spacing)
  ML    a long (multi-line) value that came back short, or cut off
  LOC   the value is not in the window the field was read in (it is elsewhere on the page, or the window holds a neighbour's value,
        or no alignment was found for the page)
  REC   the value IS in the window and the read still differs (a misread character)
  UNK   none of these

`LOC` needs where the words are: a PDF's text layer for a text-native page, otherwise one whole-page Tesseract read of the same rendering the
fields were read from. A word is "in" a window when its centre is inside it (raster) or when at least half of it is (text layer).
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402
from pytesseract import Output  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402

from app import runtime  # noqa: E402
from app.config import TESSERACT_CMD  # noqa: E402

runtime.configure_tesseract()
pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

from app.modules import pipeline  # noqa: E402
from app.modules.field_extraction import _fields_on_page  # noqa: E402
from app.modules.preprocessing import preprocess_image  # noqa: E402
from app.modules.template_store import get_template_version  # noqa: E402
from app.modules.type_identification import identify_document_type  # noqa: E402
from app.modules.validation import has_validator  # noqa: E402
from export_batch_json import same  # noqa: E402


def toks(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(s).lower())


def squash(s: str) -> str:
    return "".join(toks(s))


def num(s: str):
    try:
        return float(str(s).replace(",", "").strip())
    except ValueError:
        return None


def has(token: str, words: list[str]) -> bool:
    return any(token == w or (len(token) >= 4 and fuzz.ratio(token, w) >= 85) for w in words)


def share(truth: list[str], words: list[str]) -> float:
    return sum(has(t, words) for t in truth) / len(truth) if truth else 1.0


def page_words_raster(image_bytes: bytes):
    img = Image.open(io.BytesIO(image_bytes))
    img.load()
    data = pytesseract.image_to_data(img, config="--psm 11", output_type=Output.DICT)
    out = []
    for i, text in enumerate(data["text"]):
        if not str(text).strip():
            continue
        try:
            if float(data["conf"][i]) < 0:
                continue
        except (TypeError, ValueError):
            continue
        x0, y0 = data["left"][i] / img.width, data["top"][i] / img.height
        out.append((str(text), (x0, y0, x0 + data["width"][i] / img.width, y0 + data["height"][i] / img.height)))
    return out


def words_in(box, words, text_layer: bool):
    x0, y0, x1, y1 = box
    inside = []
    for text, (a, b, c, d) in words:
        if text_layer:
            area = (c - a) * (d - b)
            inter = max(0.0, min(x1, c) - max(x0, a)) * max(0.0, min(y1, d) - max(y0, b))
            ok = area > 0 and inter / area >= 0.5
        else:
            ok = x0 <= (a + c) / 2 <= x1 and y0 <= (b + d) / 2 <= y1
        if ok:
            inside.append(text)
    return [t for w in inside for t in toks(w)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs-dir", type=Path, required=True)
    ap.add_argument("--ground-truth", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--extracted", type=Path, required=True)
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))
    kinds = json.loads(args.manifest.read_text(encoding="utf-8"))["kinds"]
    extracted = json.loads(args.extracted.read_text(encoding="utf-8"))
    rows, docs = [], []
    names = sorted(truth)[: args.limit]
    for name in names:
        want_fields = truth[name]["fields"]
        kind = kinds[name]
        entry = extracted.get(name, {})
        page0 = (entry.get("pages") or [{}])[0]
        if not page0.get("confirmed"):
            docs.append({"file": name, "kind": kind, "identified": False, "reason": page0.get("reason")})
            for fid in want_fields:
                rows.append({"file": name, "kind": kind, "field": fid, "cause": "ID", "right": False, "tier": None})
            continue
        got_fields = page0["fields"]
        buf = (args.docs_dir / name).read_bytes()
        pages, _meta, layers = pipeline.prepare_document(buf, name, None)
        generic = preprocess_image(pages[0], deskew=layers[0] is None)
        ident = identify_document_type(generic.image_bytes, None)
        template = get_template_version(ident.template_id, ident.version)
        t0 = time.time()
        ex = pipeline.extract_page_fields(template, pages[0], generic, layers[0], ident.alignment)
        seconds = time.time() - t0
        windows = {f.id: f.bbox for f in _fields_on_page(template, ex.alignment)}
        template_fields = {f.id: f for f in template.fields}
        native = ex.text_native and layers[0] is not None
        words = [(w.text, w.bbox) for w in layers[0].words] if native else page_words_raster(ex.review_image_bytes)
        page_tokens = [t for w, _ in words for t in toks(w)]
        all_true = [t for v in want_fields.values() for t in toks(v)]
        page_found = share(all_true, page_tokens)
        docs.append({"file": name, "kind": kind, "identified": True, "template": ident.template_id, "reader": ex.reader, "aligned": ex.alignment is not None,
                     "page_true_words_found": round(page_found, 3), "extract_seconds": round(seconds, 2),
                     "values_match_api": sum(1 for f, g in ex.fields.items() if str(g.raw_text) == str(got_fields.get(f, {}).get("value", g.raw_text)))})
        for fid, want in want_fields.items():
            g = got_fields.get(fid)
            if g is None:
                continue
            got, tier = str(g["value"]), g["approval_tier"]
            ok = same(fid, got, want)
            tf = template_fields.get(fid)
            row = {"file": name, "kind": kind, "field": fid, "tier": tier, "right": ok, "got": got, "want": str(want), "reasons": g.get("reasons")}
            if ok:
                if tier == "auto":
                    continue
                row["cause"] = "GATE" if tf is not None and has_validator(tf) else "VAL"
                rows.append(row)
                continue
            gt = toks(want)
            win = words_in(windows[fid], words, native) if fid in windows else []
            in_win, on_page = share(gt, win), share(gt, page_tokens)
            neighbours = any(o != fid and len(toks(v)) and share(toks(v)[:3], win) == 1.0 and not share(toks(v)[:3], gt) for o, v in want_fields.items() if str(v).strip())
            if not native and page_found < 0.30:
                cause = "PRE"
            elif squash(got) == squash(want) or (num(got) is not None and num(got) == num(want)) or (squash(want) and squash(want) in squash(got) and len(squash(got)) <= len(squash(want)) + 3 and got.strip()):
                cause = "FMT"
            elif len(str(want)) >= 25 and got.strip() and share(toks(got), toks(want)) >= 0.8 and share(gt, toks(got)) < 0.9:
                cause = "ML"
            elif ex.alignment is None and not native and in_win < 0.5:
                cause = "LOC"
            elif in_win < 0.5 and (on_page >= 0.5 or neighbours):
                cause = "LOC"
            elif in_win >= 0.5:
                cause = "REC"
            elif not str(want).strip():
                cause = "REC"  # a blank value that got an answer: the window read ink that is not the value
            else:
                cause = "UNK"
            row["cause"] = cause
            rows.append(row)
    out = {"docs": docs, "fields": rows}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
