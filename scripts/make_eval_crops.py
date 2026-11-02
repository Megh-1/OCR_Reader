"""
Cuts every field of the named real documents at all widening levels into a sample set the GPU reader can run (jsonl + crops.zip).

  IDP_FORM=B IDP_LANDMARKS=scripts/landmarks/air_waybill_bkk_trainonly.json python scripts/make_eval_crops.py train_B 02 06 08 11 12
  IDP_FORM=B IDP_LANDMARKS=scripts/landmarks/air_waybill_bkk_trainonly.json python scripts/make_eval_crops.py test_B --final 01 03 13 14

Held-out documents (split.json "test") are refused unless --final is given: they are for ONE evaluation, after the model is fixed.
No answers are written - scoring against the labels happens in scripts/score_vlm.py. A document the alignment cannot place is listed
in `refused.json` beside the set: the system refuses it rather than reading it with boxes in the wrong place.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "gpu"))

from scripts import make_crops as C  # noqa: E402
from prompts import prompt_for  # noqa: E402

ROOT = C.ROOT
FORM = C.P.FORM


def main() -> None:
    args = sys.argv[1:]
    name, final, any_form = args[0], "--final" in args, "--any-form" in args
    docs = [a for a in args[1:] if not a.startswith("--")]
    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))["templates"]
    test = {n for t in split.values() for n in t["test"]}
    held = [d for d in docs if d in test]
    if held and not final:
        sys.exit(f"documents {held} are held out; pass --final to read them (once, after the model is fixed)")
    out = ROOT / "work" / f"evalset_{name}"
    out.mkdir(parents=True, exist_ok=True)
    C.load_reference_printed()
    samples, refused = [], []
    for num in docs:
        lab = next((ROOT / "labels").glob(f"{num}*.json"))
        doc = json.loads(lab.read_text(encoding="utf-8"))
        if not doc["form"].startswith(FORM) and not any_form:
            sys.exit(f"{lab.name} is a {doc['form']} document, not form {FORM}")
        pdf = next(ROOT.glob(f"*/{doc['file']}"))
        page, al = C.align_page(pdf.read_bytes(), pdf.name)
        if al is None:
            refused.append(num)
            print(f"{num}: not aligned - refused")
            continue
        for e in C.crops_for_page(page, al, out / "crops", num):
            samples.append({**e, "set": name, "prompt": prompt_for(e)})
        print(f"{num}: {sum(1 for s in samples if s['doc'] == num)} crops", flush=True)
    (out / "eval.jsonl").write_text("\n".join(json.dumps(s, ensure_ascii=False) for s in samples) + "\n", encoding="utf-8")
    (out / "refused.json").write_text(json.dumps(refused), encoding="utf-8")
    print(f"{name}: {len(samples)} crops from {len(docs) - len(refused)} documents; refused {refused or 'none'} -> {out}")


if __name__ == "__main__":
    main()
