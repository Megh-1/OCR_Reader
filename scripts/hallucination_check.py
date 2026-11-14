#!/usr/bin/env python
"""
A hallucination check for a finished batch: values the reader answered that the page does not support.

    python scripts/hallucination_check.py --extracted data/loop/baseline_dev/extracted.json --docs-dir data/demo_50 \
        --ground-truth data/demo_50/ground_truth.json --manifest data/demo_50/manifest.json --out data/loop/baseline_dev [--aggregate-only]

Per input kind (digital / scan / photo), counts of
  blank_answered   a field whose ground truth is EMPTY (the crop is blank) that got a non-empty answer;
  unsupported      a non-empty answer in which a word of three or more letters and digits is found on NO word of the page's own text
                   (its PDF text layer plus one whole-page Tesseract read of the render), allowing a one-character slip per four;
  untestable       a non-empty answer with no word that long;
and the same two counts for the answers that reached `auto`. They are indicators, not proofs: Tesseract can miss a word that is printed,
and the reader can be wrong with words that are on the page (a neighbour's value). A value that is not on the page at all is the stronger
signal. Written to hallucination.json beside the batch's scorecard; this is a separate file on purpose, so the scoring scripts stay as they are.
`--aggregate-only` (for the gold and hold-out sets) writes and prints the counts and nothing per file or per field.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import fitz  # noqa: E402
import pytesseract  # noqa: E402
from PIL import Image  # noqa: E402
from rapidfuzz import fuzz  # noqa: E402

from app.config import PDF_RENDER_DPI, TESSERACT_CMD  # noqa: E402

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 3]


def page_words(path: Path) -> set[str]:
    words: set[str] = set()
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as doc:
            page = doc[0]
            words |= set(tokens(page.get_text()))
            pix = page.get_pixmap(dpi=PDF_RENDER_DPI)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    else:
        img = Image.open(path)
        img.load()
    words |= set(tokens(pytesseract.image_to_string(img, config="--psm 11")))
    return words


def supported(token: str, words: set[str]) -> bool:
    if token in words:
        return True
    slack = max(1, len(token) // 4)
    return any(abs(len(w) - len(token)) <= slack and fuzz.ratio(token, w) >= 100 * (1 - slack / max(len(token), 1)) for w in words)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--extracted", type=Path, required=True)
    ap.add_argument("--docs-dir", type=Path, required=True)
    ap.add_argument("--ground-truth", type=Path, required=True)
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--aggregate-only", action="store_true")
    args = ap.parse_args()

    extracted = json.loads(args.extracted.read_text(encoding="utf-8"))
    truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))
    kinds = json.loads(args.manifest.read_text(encoding="utf-8"))["kinds"] if args.manifest else {}
    counts: dict[str, Counter] = defaultdict(Counter)
    detail = []
    for name, entry in sorted(extracted.items()):
        if entry.get("status") != "completed" or not entry["pages"] or not entry["pages"][0].get("confirmed"):
            continue
        kind = kinds.get(name, "?")
        fields = entry["pages"][0]["fields"]
        words = page_words(args.docs_dir / name)
        for fid, want in truth.get(name, {}).get("fields", {}).items():
            got = fields.get(fid)
            if got is None or not str(got["value"]).strip():
                continue
            tier = got["approval_tier"]
            c = counts[kind]
            c["answered"] += 1
            blank = not str(want).strip()
            toks = tokens(str(got["value"]))
            flags = []
            if blank:
                flags.append("blank_answered")
            if not toks:
                flags.append("untestable")
            elif not all(supported(t, words) for t in toks):
                flags.append("unsupported")
            for f in flags:
                c[f] += 1
                if tier == "auto":
                    c[f + "_auto"] += 1
            if flags and not args.aggregate_only:
                detail.append({"file": name, "field": fid, "tier": tier, "flags": flags, "value": got["value"]})
    out = {"by_input_kind": {k: dict(v) for k, v in sorted(counts.items())}}
    if not args.aggregate_only:
        out["detail"] = detail
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "hallucination.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out["by_input_kind"], indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
