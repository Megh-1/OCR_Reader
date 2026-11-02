"""
Scores a whole-page vision run (scripts/gpu/vlm_read.py over scripts/make_page_sets.py output) against the labels.

  python scripts/score_page.py results/pages_test_zero.jsonl [--final] [--show-wrong]

One reader and no template: per document, how many fields equal the label, how many are WRONG yet non-empty (a confident wrong value
is the costly kind), and how many such values would also have passed the field's format check - the ones a format rule alone cannot
catch. Documents of the held-out list need --final (and the others are skipped with it, so the two never mix).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import waybill_forms as wf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "data" / "real_awb"
FIELDS = {f.id: f for f in wf.AIR_WAYBILL_BKK.fields}


def norm(s) -> str:
    return " ".join(str(s or "").split())


def selected(fid: str, raw: str) -> str:
    from app.modules.field_extraction import _select
    f = FIELDS[fid]
    return _select(f, raw)[0] if raw and (f.extract_pattern or f.squash_spaces) and f.from_field is None else raw


def parse(reply: str) -> dict:
    m = re.search(r"\{.*\}", reply, flags=re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--show-wrong", action="store_true")
    args = ap.parse_args()
    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))["templates"]
    test = {n for t in split.values() for n in t["test"]}
    tot = Counter()
    for line in Path(args.results).read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if (e["doc"] in test) != args.final:
            continue
        lab = json.loads(next((ROOT / "labels").glob(f"{e['doc']}*.json")).read_text(encoding="utf-8"))["fields"]
        got = parse(e["reply"])
        c = Counter()
        for fid, want in lab.items():
            v = selected(fid, norm(got.get(fid, "")))
            rx = FIELDS[fid].validation_regex
            ok = norm(v) == norm(want)
            c["n"] += 1; c["right"] += ok
            wrong = (not ok) and bool(v)
            c["wrong_nonempty"] += wrong
            c["wrong_but_valid"] += wrong and (rx is None or bool(re.fullmatch(rx, v)))
            if args.show_wrong and not ok:
                print(f"   {e['doc']} {fid:28s} want {norm(want)[:40]!r:44s} got {v[:40]!r}")
        tot.update(c)
        print(f"{e['doc']} form {e.get('form', '?')}: right {c['right']:2d}/{c['n']}  wrong-and-non-empty {c['wrong_nonempty']:2d}  (format-valid anyway: {c['wrong_but_valid']})")
    print(f"TOTAL right {tot['right']}/{tot['n']}  wrong-and-non-empty {tot['wrong_nonempty']}  format-valid wrong {tot['wrong_but_valid']}")


if __name__ == "__main__":
    main()
