"""
Scores a vision-model run over ready samples (trainset/*.jsonl, which carry their exact answers) - the synthetic validation set.

  python scripts/score_samples.py data/real_awb/work/results/base_synval.jsonl [more.jsonl ...]
  IDP_FORM=B python scripts/score_samples.py --staged data/synthetic_bkk_val results/x.jsonl     # all 3 windows, staged, vs ground truth

Per field: how often the model's answer equals the ground truth (whitespace-normalised), and how often it is EMPTY when it should
be. Several files are printed side by side so an untuned and a tuned run can be compared on the same crops.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path


def norm(s) -> str:
    return " ".join(str(s or "").split())


def parse(reply: str):
    m = re.search(r"\{.*\}", reply, flags=re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def fields_of(entry: dict, text: str) -> dict[str, str]:
    """{field: value} out of either the reply or the stored answer, whichever `text` is"""
    empty = lambda v: "" if norm(v).upper() in ("EMPTY", "") else norm(v)  # noqa: E731
    if entry["kind"] == "single":
        return {entry["fields"][0]: empty(text.strip().strip("`"))}
    j = parse(text)
    if entry["kind"] == "party":
        return {entry["fields"][0]: empty(j.get("name", "")), entry["fields"][1]: empty(j.get("address", ""))}
    return {"nature_of_goods": empty(j.get("description", "")), "hs_code": empty(j.get("hs_code", "")), "invoice_reference": empty(j.get("invoice_number", ""))}


def score(path: Path):
    right, total = defaultdict(int), defaultdict(int)
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        want, got = fields_of(e, e["answer"]), fields_of(e, e["reply"])
        for f, w in want.items():
            total[f] += 1
            right[f] += norm(got.get(f, "")) == norm(w)
    return right, total


def staged_report(path: Path, truth_dir: Path) -> None:
    """The deployed behaviour on the synthetic validation pages: tightest window first, wider only when nothing usable came back,
    judged against each page's exact ground truth (not against the per-window answers, which say EMPTY when the value is outside).
    Needs IDP_FORM for the template's formats."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import score_vlm as S
    from scripts.make_train_set import canonical

    truth = json.loads((truth_dir / "ground_truth.json").read_text(encoding="utf-8"))
    by = defaultdict(lambda: defaultdict(dict))
    for line in path.read_text(encoding="utf-8").splitlines():
        e = json.loads(line)
        for f, v in fields_of(e, e["reply"]).items():
            by[e["doc"]][f][e["level"]] = v
    tot = defaultdict(int)
    per = defaultdict(lambda: defaultdict(int))
    for doc, fields in by.items():
        key = "_".join(doc.split("_")[-2:]) + "__scan.pdf"  # syn_val_bkk_0001 and syn_val_hard_bkk_0001 are both bkk_0001
        if key not in truth:
            continue
        want = canonical(truth[key]["fields"])
        got = {f: S.staged(f, lv) for f, lv in fields.items()}
        corroborated = S.checked_fields({f: g[0] for f, g in got.items()})
        for f, (v, lvl, valid) in got.items():
            w = S.norm(want.get(f, ""))
            ok = S.norm(v) == w
            ok0 = S.norm(S.selected(f, fields[f].get(0, ""))) == w
            tot["n"] += 1; tot["staged"] += ok; tot["first"] += ok0
            tot["wrong_nonempty"] += (not ok) and bool(v)
            gate = f in corroborated and S.passes_format(f, v)
            tot["gated"] += gate; tot["gated_wrong"] += gate and not ok
            per[f]["n"] += 1; per[f]["staged"] += ok; per[f]["first"] += ok0
    n = max(1, tot["n"])
    print(f"{path.name}: {tot['n']} fields   first window {tot['first']} ({100 * tot['first'] / n:.1f}%)   staged {tot['staged']} ({100 * tot['staged'] / n:.1f}%)   "
          f"wrong-and-non-empty {tot['wrong_nonempty']}   check-corroborated + format ok {tot['gated']}, of which wrong {tot['gated_wrong']}")
    weak = sorted(per.items(), key=lambda kv: kv[1]["staged"] / kv[1]["n"])[:8]
    print("weakest fields (staged / n): " + "  ".join(f"{f} {v['staged']}/{v['n']}" for f, v in weak))


def main() -> None:
    if sys.argv[1] == "--staged":  # --staged GROUND_TRUTH_DIR results.jsonl
        staged_report(Path(sys.argv[3]), Path(sys.argv[2]))
        return
    paths = [Path(p) for p in sys.argv[1:]]
    results = [score(p) for p in paths]
    fields = sorted(results[0][1])
    print(f"{'field':30s}" + "".join(f"{p.stem[:22]:>24s}" for p in paths))
    for f in fields:
        print(f"{f:30s}" + "".join(f"{r[f]:>14d}/{t[f]:<9d}" for r, t in results))
    print(f"{'TOTAL':30s}" + "".join(f"{sum(r.values()):>9d}/{sum(t.values()):<5d} {100 * sum(r.values()) / max(1, sum(t.values())):5.1f}%  " for r, t in results))


if __name__ == "__main__":
    main()
