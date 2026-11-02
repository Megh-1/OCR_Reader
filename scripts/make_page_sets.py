"""
Whole-page samples for a vision model: the template-free baseline ("read this waybill") that the cropped, template-guided reading is
compared against, and the only reading possible for a layout that has no template yet.

  python scripts/make_page_sets.py test_pages --final 01 03 13 14 07 16
  python scripts/make_page_sets.py train_pages 02 06 08 11 12 09 10 15 04

One sample per document: the preprocessed page image (long edge at most 2000 px) and a question listing every field of the waybill
template by its printed caption. Held-out documents need --final, as in scripts/make_eval_crops.py. Output: data/real_awb/work/pageset_NAME/
(pages.jsonl + crops.zip, the same layout scripts/gpu/vlm_read.py --samples reads).
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app.modules import pipeline, preprocessing  # noqa: E402
from scripts import waybill_forms as wf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "data" / "real_awb"
LONG_EDGE = 2000

# every layout in the corpus carries the same 33 fields; the Bangkok template's captions name them
FIELDS = {f.id: f.label for f in wf.AIR_WAYBILL_BKK.fields}


def prompt() -> str:
    lines = "\n".join(f"- {fid}: {label}" for fid, label in FIELDS.items())
    return ("This is a scan or photo of one air waybill. Read the value that is typed or written for each field below and copy it EXACTLY "
            "as it appears: no spelling fixes, no reformatting, never a guess. The form's own printed captions are not values. "
            "Answer with one JSON object whose keys are exactly these field names; use \"\" for a field that is empty or not on the page.\n"
            f"Fields:\n{lines}")


def main() -> None:
    args = sys.argv[1:]
    name, final = args[0], "--final" in args
    docs = [a for a in args[1:] if a != "--final"]
    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))["templates"]
    held = [d for d in docs if d in {n for t in split.values() for n in t["test"]}]
    if held and not final:
        sys.exit(f"documents {held} are held out; pass --final to read them")
    out = ROOT / "work" / f"pageset_{name}"
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    with zipfile.ZipFile(out / "crops.zip", "w", zipfile.ZIP_STORED) as z:
        for num in docs:
            lab = json.loads(next((ROOT / "labels").glob(f"{num}*.json")).read_text(encoding="utf-8"))
            pdf = next(ROOT.glob(f"*/{lab['file']}"))
            pages, meta, layers = pipeline.prepare_document(pdf.read_bytes(), pdf.name, None)
            page = Image.open(io.BytesIO(preprocessing.preprocess_image(pages[0], deskew=layers[0] is None).field_image_bytes)).convert("RGB")
            scale = min(1.0, LONG_EDGE / max(page.size))
            page = page.resize((int(page.width * scale), int(page.height * scale)), Image.LANCZOS)
            buf = io.BytesIO()
            page.save(buf, "PNG")
            z.writestr(f"{num}/page.png", buf.getvalue())
            rows.append({"doc": num, "kind": "page", "fields": list(FIELDS), "image": f"{num}/page.png", "level": 0, "prompt": prompt(), "form": lab["form"][:1]})
            print(f"{num}: {page.size}", flush=True)
    (out / "pages.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    print(f"{name}: {len(rows)} pages -> {out}")


if __name__ == "__main__":
    main()
