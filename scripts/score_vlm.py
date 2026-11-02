"""
Scores a vision-model run (scripts/gpu/vlm_read.py output) against the labels, next to the word-selection reader.

  IDP_FORM=B IDP_LANDMARKS=... python scripts/score_vlm.py results/x.jsonl [--final] [--show-wrong] [--evalset DIR]

Train documents only unless --final. A result file may hold several widening levels of the same crop (scripts/make_crops.py LEVELS);
the reader is STAGED: take level 0, and only when that is empty or fails the field's format look at the next, wider window - the
first value that passes is the answer, and when none passes the first non-empty one is kept (it will not be auto-approved).
Reports, per field and overall:
  vlm0           the model's answer from the first (tightest) window only
  vlm            the staged answer
  words          scripts/proto_select.py's choice of words from the page OCR
  agree          the staged answer and the word reader gave the same value
  and the number that matters for approval: of the fields where the readers AGREE or a document check (AWB check digit, weight x rate,
  charges sum) passes, and the value passes its format check, how many are wrong.  That is what an auto-approval rule would let through.
--evalset DIR adds the documents the alignment refused (DIR/refused.json): all their fields count as unread.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.modules import alignment as A  # noqa: E402
from app.modules.vlm_reader import decide as D  # noqa: E402
from scripts import make_crops as C  # noqa: E402
from scripts import proto_select as P  # noqa: E402

ROOT = P.ROOT
SPLIT_KEY = {"B": "B_bangkok_printer", "M": "M_mumbai_printer"}[P.FORM]


# The decision rules live in app/modules/vlm_reader/decide.py (what ships); these are the harness's names for them, bound to the
# template's fields so the rule that is scored is the rule that runs.
norm = D.norm
parse_json = D.parse_json
value_of = D.value_of
checked_fields = D.checked_fields


def vlm_fields(entry: dict) -> dict[str, str]:
    return D.answer_fields(entry["kind"], entry["fields"], entry["reply"])


def passes_format(fid: str, v: str) -> bool:
    return D.passes_format(P.FIELDS[fid], v)


def selected(fid: str, raw: str) -> str:
    return D.selected(P.FIELDS[fid], raw)


def staged(fid: str, per_level: dict[int, str]) -> tuple[str, int | None, bool]:
    """(value, level it came from, passed its format); see app/modules/vlm_reader/decide.py for the two corrections to the
    plain tight-first pick (extension, blank stays blank) and the synthetic-validation numbers behind them."""
    return D.staged(P.FIELDS[fid], per_level)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--show-wrong", action="store_true")
    ap.add_argument("--evalset")
    ap.add_argument("--docs", nargs="*", help="score exactly these documents, whatever the split says")
    args = ap.parse_args()

    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))["templates"][SPLIT_KEY]
    allowed = set(args.docs) if args.docs else set(split["test"] if args.final else split["train"])
    tokens_all = json.loads((ROOT / "work" / "tokens.json").read_text(encoding="utf-8"))
    printed = P.printed_words(tokens_all[C.REFERENCE]["native"])

    by = defaultdict(lambda: defaultdict(dict))  # doc -> field -> level -> raw value
    for line in Path(args.results).read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        if e["doc"] in allowed:
            for f, v in vlm_fields(e).items():
                by[e["doc"]][f][e.get("level", 0)] = v

    refused = json.loads((Path(args.evalset) / "refused.json").read_text(encoding="utf-8")) if args.evalset else []
    tot, per, lvl = Counter(), defaultdict(Counter), Counter()
    for name, rec in tokens_all.items():
        num = name[:2]
        lab = json.loads((ROOT / "labels" / name).read_text(encoding="utf-8"))["fields"]
        if num in refused:
            tot["n"] += len(lab)
            tot["refused"] += len(lab)
            print(f"{name:40s} REFUSED by the alignment: {len(lab)} fields unread")
            continue
        if num not in by:
            continue
        toks = [tuple(t) for t in (rec["ocr"])]
        words = [A.PageWord(P.key(t[0]), t[1], (t[2] + t[4]) / 2) for t in P.clean(toks) if len(re.sub(r"[^A-Za-z]", "", t[0])) >= 4 and t[5] >= 30]
        al = A.fit_alignment(P.LM, words)
        wordsel = P.read_all(toks, al, printed)
        c = Counter()
        final = {fid: staged(fid, by[num][fid])[0] if fid in by[num] else "" for fid in lab}
        corroborated = checked_fields(final)
        for fid, want in lab.items():
            if fid not in by[num]:
                continue
            v, lv, valid = staged(fid, by[num][fid])
            v0 = selected(fid, by[num][fid].get(0, ""))
            w = wordsel.get(fid, "")
            ok_v, ok_0, ok_w, agree = norm(v) == norm(want), norm(v0) == norm(want), norm(w) == norm(want), norm(v) == norm(w)
            c["n"] += 1; c["vlm"] += ok_v; c["vlm0"] += ok_0; c["words"] += ok_w; c["agree"] += agree
            gate = (agree or fid in corroborated) and passes_format(fid, v)
            c["gated"] += gate
            c["gated_wrong"] += gate and not ok_v
            lvl[lv] += 1
            per[fid]["n"] += 1; per[fid]["vlm"] += ok_v; per[fid]["words"] += ok_w
            if gate and not ok_v:
                print(f"   !! GATED WRONG {num} {fid:28s} want {norm(want)[:40]!r:44s} got {norm(v)[:40]!r}  (level {lv}, agree={agree}, check={fid in corroborated})")
            elif args.show_wrong and not ok_v:
                print(f"   {num} {fid:28s} want {norm(want)[:40]!r:44s} vlm {norm(v)[:40]!r:44s} words {norm(w)[:30]!r}  level {lv}")
        for k, v in c.items():
            tot[k] += v
        print(f"{name:40s} fields {c['n']:2d}  vlm0 {c['vlm0']:2d}  vlm {c['vlm']:2d}  words {c['words']:2d}  agree {c['agree']:2d}  gated {c['gated']:2d} (wrong among gated: {c['gated_wrong']})")
    n = max(1, tot["n"])
    print(f"\nOVER {tot['n']} fields ({tot['refused']} unread because the document was refused): "
          f"vlm, first window {tot['vlm0']} ({100 * tot['vlm0'] / n:.0f}%)   vlm, staged {tot['vlm']} ({100 * tot['vlm'] / n:.0f}%)   "
          f"word-selection {tot['words']} ({100 * tot['words'] / n:.0f}%)   readers agree {tot['agree']} ({100 * tot['agree'] / n:.0f}%)")
    print(f"(readers agree OR an arithmetic check passes) AND format ok ('auto'): {tot['gated']} ({100 * tot['gated'] / n:.0f}% of fields)   of which WRONG: {tot['gated_wrong']}")
    print("staged answers by the window they came from: " + "  ".join(f"level {k}: {v}" for k, v in sorted(lvl.items(), key=lambda kv: (kv[0] is None, kv[0]))))
    print("\nper field (vlm / words / n): " + "  ".join(f"{k}={v['vlm']}/{v['words']}/{v['n']}" for k, v in per.items()))


if __name__ == "__main__":
    main()
