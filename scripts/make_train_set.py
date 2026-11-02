"""
Builds the vision model's training data from page crops: (crop, question, answer) triples.

  syn_train   50 synthetic scans of the form (scripts/synth_bkk.py or generate_synthetic_awb.py), answers from their exact ground truth
  syn_val     20 more with a different seed - never trained on, the development check
  real_train  the real training pages of the frozen split, answers from the verified labels

The held-out real documents (data/real_awb/split.json "test") are never read here.

WHAT THE ANSWER DEPENDS ON. A crop is a search window on the page, and a value that was typed outside it (print drift) is not in the
picture. Answering with the true value for such a crop teaches the model to invent a plausible one - measured: it wrote 123,456.00
for charges crops that showed only captions. So the answer is made to match the picture:
  value fully inside the window   -> the value
  value not inside it             -> EMPTY  (the reader then widens the window, scripts/make_crops.py LEVELS)
  value partly cut off            -> the sample is skipped: neither answer would be honest
Where the page came from the generator, "inside" is exact (it records where it typed each value); for a real page it is judged from
the page's own words (text layer, or the OCR), and a real sample whose value cannot be found in the window is skipped, not turned
into EMPTY, because OCR missing a word is not evidence that the word is absent.

  IDP_FORM=B|M python scripts/make_train_set.py [syn_train syn_val real_train]
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "gpu"))

from scripts import make_crops as C  # noqa: E402
from prompts import prompt_for  # noqa: E402

ROOT = C.ROOT
FORM = C.P.FORM
ROUND = os.environ.get("IDP_ROUND", "")  # "4": the varied data of scripts/synth_formats.py, in its own folder
OUT = ROOT / "work" / (f"trainset{ROUND}" if FORM == "B" else f"trainset{ROUND}_M")
SPLIT_KEY = {"B": "B_bangkok_printer", "M": "M_mumbai_printer"}[FORM]
# syn_hard / syn_val_hard: the same generators with --hard (worse print misregistration, worse scanner); trained on, and kept apart in
# validation so the plain syn_val numbers stay comparable with the first round.
SYNTHETIC = {"B": (("syn_train", "synthetic_bkk"), ("syn_hard", "synthetic_bkk_hard"), ("syn_val", "synthetic_bkk_val"), ("syn_val_hard", "synthetic_bkk_val_hard")),
             "M": (("syn_train", "synthetic_m"), ("syn_hard", "synthetic_m_hard"), ("syn_val", "synthetic_m_val"), ("syn_val_hard", "synthetic_m_val_hard"))}[FORM]
if ROUND:  # trained on the varied pages only; the first round's validation pages are re-cut too, so rounds compare on the same documents
    _p = {"B": "synthetic_bkk", "M": "synthetic_m"}[FORM]
    SYNTHETIC = (("syn_train", f"{_p}{ROUND}"), ("syn_hard", f"{_p}{ROUND}_hard"), ("syn_val", f"{_p}_val"), ("syn_val_hard", f"{_p}_val_hard"),
                 (f"syn_val{ROUND}", f"{_p}{ROUND}_val"), (f"syn_val{ROUND}_hard", f"{_p}{ROUND}_val_hard"))
PROJECT = ROOT.parent.parent
rnd = random.Random(3)


def canonical(f: dict) -> dict:
    """The reference-form generator writes the HS code and invoice the way the reference prints them ("HS CODE: 84159000",
    "INV: EX/KA/0057/24-25"); the labels, and so the model's answers, carry the code and the number alone."""
    f = dict(f)
    f["hs_code"] = re.sub(r"^\s*HS\s*CODE\s*[:.]?\s*", "", f.get("hs_code", ""), flags=re.I)
    f["invoice_reference"] = re.sub(r"^\s*INV\s*[:.]?\s*", "", f.get("invoice_reference", ""), flags=re.I)
    return f


def inside(box, region) -> float:
    """share of `box` that lies inside `region`"""
    ix = max(0.0, min(box[2], region[2]) - max(box[0], region[0]))
    iy = max(0.0, min(box[3], region[3]) - max(box[1], region[1]))
    area = max(1e-9, (box[2] - box[0]) * (box[3] - box[1]))
    return ix * iy / area


def tri(share: float) -> str:
    """fully in the window, out of it, CLIPPED (plainly cut: the model must say EMPTY so that the reader widens, measured: it copied the
    visible part, 1432.0 for 1432.00, and the format check passed it) or partial (too close to call, the sample is skipped)"""
    if ROUND:
        return "in" if share >= 0.97 else "out" if share <= 0.03 else "clipped" if share <= 0.90 else "partial"
    return "in" if share >= 0.6 else "out" if share <= 0.1 else "partial"


def synthetic_visibility(fid: str, boxes: dict, region) -> str:
    box = boxes.get(fid) or (boxes.get("nature_of_goods") if fid in ("hs_code", "invoice_reference") else None)
    return tri(inside(box, region)) if box else "in"  # a blank field has nothing to hide


def real_visibility(value: str, region, tokens) -> str:
    """judged from the page's own words: the share of the value's words that are found inside the window"""
    words = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in value.split()]
    words = [w for w in words if w]
    if not words:
        return "in"
    keys = {re.sub(r"[^a-z0-9]", "", t[0].lower()) for t in tokens
            if region[0] <= (t[1] + t[3]) / 2 <= region[2] and region[1] <= (t[2] + t[4]) / 2 <= region[3]}
    if ROUND:  # one of the value's own words cut by the window's edge
        for t in tokens:
            if re.sub(r"[^a-z0-9]", "", t[0].lower()) in words and len(words) <= 3 and 0.1 < inside(t[1:5], region) < 0.9:
                return "clipped"
    return "in" if sum(w in keys for w in words) / len(words) >= 0.5 else "unknown"


def answer(entry: dict, f: dict, vis: dict[str, str]) -> str | None:
    """the answer that matches the picture, or None when the picture does not settle it"""
    states = {fid: vis.get(fid, "in") for fid in entry["fields"]}
    if "partial" in states.values() or "unknown" in states.values():
        return None
    if "clipped" in states.values() and entry["kind"] != "single":  # a many-line cell: part of it may be perfectly readable
        return None
    val = {fid: (f.get(fid, "") if st == "in" else "") for fid, st in states.items()}  # out of the window or clipped: EMPTY
    if entry["kind"] == "single":
        return val[entry["fields"][0]] or "EMPTY"
    if entry["kind"] == "party":
        n, a = entry["fields"]
        return json.dumps({"name": val[n], "address": val[a]}, ensure_ascii=False)
    return json.dumps({"description": val["nature_of_goods"], "hs_code": val["hs_code"] or "EMPTY",
                       "invoice_number": val["invoice_reference"] or "EMPTY"}, ensure_ascii=False)


def build(set_name: str, items: list[tuple], all_levels: bool, tokens_by_doc: dict | None = None) -> None:
    samples, skipped, empties = [], 0, 0
    for doc_id, data, filename, fields, boxes in items:
        page, al = C.align_page(data, filename)
        if al is None:
            print(f"  {set_name}/{doc_id}: no alignment, skipped")
            continue
        for e in C.crops_for_page(page, al, OUT / "crops", f"{set_name}_{doc_id}"):
            vis = {}
            for fid in e["fields"]:
                if boxes is not None:
                    vis[fid] = synthetic_visibility(fid, boxes, e["region"])
                else:
                    vis[fid] = real_visibility(fields.get(fid, ""), e["region"], (tokens_by_doc or {}).get(doc_id, []))
            ans = answer(e, fields, vis)
            if ans is None:
                skipped += 1
                continue
            samples.append({**e, "set": set_name, "prompt": prompt_for(e), "answer": ans})
            empties += ans == "EMPTY" or '"name": "", "address": ""' in ans
        print(f"  {set_name}/{doc_id}", flush=True)
    # training keeps every level-0 crop and ONE random wider level per field (so it also sees the wider windows, at a steady size);
    # validation keeps all levels, since the reader will widen step by step
    chosen = list(samples) if all_levels else [s for s in samples if s["level"] == 0]
    if not all_levels:
        by_key: dict[tuple, list] = {}
        for s in samples:
            if s["level"] > 0:
                by_key.setdefault((s["doc"], tuple(s["fields"])), []).append(s)
        chosen += [rnd.choice(v) for v in by_key.values()]
    (OUT / f"{set_name}.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in chosen) + "\n", encoding="utf-8")
    print(f"{set_name}: {len(chosen)} samples ({skipped} partly-cut or unconfirmed crops skipped, {empties} EMPTY answers among all levels)")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    C.load_reference_printed()
    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))["templates"][SPLIT_KEY]
    only = set(sys.argv[1:])
    for syn, directory in SYNTHETIC:
        if only and syn not in only:
            continue
        d = PROJECT / "data" / directory
        truth = json.loads((d / "ground_truth.json").read_text(encoding="utf-8"))
        items = [(name[:8], (d / name).read_bytes(), name, canonical(t["fields"]), t["boxes"]) for name, t in sorted(truth.items()) if name.endswith("__scan.pdf")]
        build(syn, items, all_levels=syn.startswith("syn_val"))
    if only and "real_train" not in only:
        return
    tokens_all = json.loads((ROOT / "work" / "tokens.json").read_text(encoding="utf-8"))
    real, tok = [], {}
    for lab in sorted((ROOT / "labels").glob("*.json")):
        num = lab.name[:2]
        if num not in split["train"]:  # held-out and unassigned documents never enter training
            continue
        doc = json.loads(lab.read_text(encoding="utf-8"))
        if not doc["form"].startswith(FORM):
            continue
        pdf = next(ROOT.glob(f"*/{doc['file']}"))
        real.append((num, pdf.read_bytes(), pdf.name, doc["fields"], None))
        rec = tokens_all[lab.name]
        tok[num] = rec["native"] or rec["ocr"]
    build("real_train", real, all_levels=False, tokens_by_doc=tok)


if __name__ == "__main__":
    main()
