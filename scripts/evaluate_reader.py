"""
Scores reader engines on the same labelled documents, so a model is chosen by numbers and not by its press.

    python scripts\\evaluate_reader.py --images-dir data\\demo_50 --ground-truth data\\demo_50\\ground_truth.json \\
        --engine tesseract --engine qwen_waybill --engine candidate_vlm [--limit 12] [--out data\\reader_scorecard.json]

Each `--engine` is a name from the registry (READER_ENGINES_FILE, app/modules/vlm_reader/engines.py), or `tesseract` for the baseline with
no reader at all. The documents go through the real pipeline path (scripts/evaluate_accuracy.py: ingestion, preprocessing, Module 5,
pipeline.extract_page_fields, Module 7), once per engine, in this process - so the engine is whatever the registry file says, and the
service behind it must be running.

Per engine and input kind it prints fields right, wrong values by approval tier (a wrong value in `spot_check` costs a reviewer's
attention; one in `auto` would ship), and documents per minute. With two or more engines it also prints the SECOND-READER table: where
two engines give the same value, how often is it right? That is the number that decides whether one engine can vouch for another and so
shrink the review queue (Tesseract failed this test: it agreed with the model on 55 of 624 uncertain values).

--save stores each engine's result PER TEMPLATE in the database (engine_scorecards, and engine_pairs for every two engines): the
evidence engine selection ranks by (docs/engine-selection.md). Two scores are comparable only on the same documents, so each is stamped
with a corpus id (a hash of the file names, their templates and --limit). --promote then judges every non-baseline engine for each
template with app/modules/engine_selection/promotion.py - it must beat the incumbent (a promoted engine, or `tesseract`) by the margin
on THIS corpus, so run the baseline and any incumbent in the same command - and records the promotions that pass.

A value is right when it matches the label after whitespace and case normalisation, ignoring the printed `HS CODE:` / `INV:` label the
template drops on purpose (export_batch_json.same). Only page 1 of a file is scored, as in evaluate_accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_accuracy as EA  # noqa: E402  (configures Tesseract, imports the pipeline)
from app import config  # noqa: E402
from app.modules.validation import approval_tier  # noqa: E402
from export_batch_json import same  # noqa: E402
from app.modules.engine_selection import promotion, selector, store  # noqa: E402

BASELINE = "tesseract"


def select_engine(name: str) -> None:
    """Points the pipeline at one engine for the runs that follow. `tesseract` turns the vision reader off altogether."""
    if name == BASELINE:
        config.VLM_READER_URL = ""
        config.READER_ENGINES_FILE = ""
        config.READER_ENGINE = ""
    else:
        if not config.READER_ENGINES_FILE:
            raise SystemExit(f"engine {name!r} needs READER_ENGINES_FILE (see config/reader_engines.example.json)")
        config.READER_ENGINE = name


def run_engine(name: str, images_dir_gt: list[tuple[Path, dict]], limit: int | None) -> dict:
    select_engine(name)
    started = time.time()
    results: list[EA.FileResult] = []
    for images_dir, ground_truth in images_dir_gt:
        items = list(ground_truth.items())[:limit] if limit else list(ground_truth.items())
        results.extend(EA.evaluate_all(images_dir, dict(items)))
    wall = time.time() - started

    fields: dict[tuple[str, str], dict] = {}
    kinds: dict[str, dict] = defaultdict(lambda: {"fields": 0, "right": 0, "wrong": defaultdict(int)})
    errors = []
    templates: dict[str, str] = {}
    for r in results:
        templates[r.filename] = r.expected_template
        if r.error:
            errors.append(f"{r.filename}: {r.error}")
        kind = r.tier or "unknown"
        for f in r.fields:
            right = same(f.field_id, f.actual, f.expected)
            tier = approval_tier(f.accepted, f.confidence, f.has_validator)  # the app's own rule
            fields[(r.filename, f.field_id)] = {"value": f.actual, "right": right, "tier": tier}
            k = kinds[kind]
            k["fields"] += 1
            k["right"] += right
            if not right:
                k["wrong"][tier] += 1
    docs = sum(1 for r in results if r.fields)
    return {"engine": name, "documents": len(results), "scored_documents": docs, "wall_s": round(wall, 1), "templates": templates,
            "docs_per_min": round(60 * docs / wall, 2) if wall else 0.0, "errors": errors, "fields": fields,
            "by_kind": {k: {"fields": v["fields"], "right": v["right"], "wrong": dict(v["wrong"])} for k, v in kinds.items()}}


def print_engine(run: dict) -> None:
    total = sum(v["fields"] for v in run["by_kind"].values())
    right = sum(v["right"] for v in run["by_kind"].values())
    wrong_tier: dict[str, int] = defaultdict(int)
    for v in run["by_kind"].values():
        for t, n in v["wrong"].items():
            wrong_tier[t] += n
    print(f"\n== {run['engine']}: {right}/{total} fields right ({100 * right / total:.1f}%)" if total else f"\n== {run['engine']}: nothing scored",
          f"| {run['scored_documents']}/{run['documents']} documents scored | {run['docs_per_min']} docs/min")
    for kind, v in sorted(run["by_kind"].items()):
        print(f"   {EA.TIER_LABELS.get(kind, kind):<28} {v['right']:>5}/{v['fields']:<5} {100 * v['right'] / v['fields']:5.1f}%   wrong by tier: {v['wrong']}")
    print(f"   wrong values that would ship unchecked (auto): {wrong_tier.get('auto', 0)}   waiting for a person (spot_check): {wrong_tier.get('spot_check', 0)}   already flagged (review): {wrong_tier.get('review', 0)}")
    for e in run["errors"][:5]:
        print("   error:", e)


def print_pair(a: dict, b: dict) -> None:
    both = [k for k in a["fields"] if k in b["fields"]]
    agree = [k for k in both if a["fields"][k]["value"].strip() and " ".join(a["fields"][k]["value"].split()).lower() == " ".join(b["fields"][k]["value"].split()).lower()]
    agree_right = sum(a["fields"][k]["right"] for k in agree)
    differ = [k for k in both if k not in set(agree)]
    print(f"\n== second reader: {a['engine']} vs {b['engine']} on {len(both)} fields")
    print(f"   same value: {len(agree)} ({100 * len(agree) / len(both):.1f}%), of which right {agree_right}, WRONG {len(agree) - agree_right}")
    print(f"   different:  {len(differ)}: only {a['engine']} right {sum(a['fields'][k]['right'] and not b['fields'][k]['right'] for k in differ)}, "
          f"only {b['engine']} right {sum(b['fields'][k]['right'] and not a['fields'][k]['right'] for k in differ)}, "
          f"neither {sum(not a['fields'][k]['right'] and not b['fields'][k]['right'] for k in differ)}")
    if len(agree) - agree_right:
        print("   (agreement is only a gate if 'WRONG' is tiny: errors the two engines share are invisible to each other)")


def corpus_id(corpus: list[tuple[Path, dict]], limit: int | None) -> str:
    """Names the documents a score was measured on: the same files, templates and cut-off give the same id."""
    keys = sorted(f"{name}|{gt['template_id']}" for _, ground_truth in corpus for name, gt in (list(ground_truth.items())[:limit] if limit else ground_truth.items()))
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


def per_template(run: dict, corpus: str) -> dict[str, tuple[selector.Card, int, dict]]:
    """One engine's run as a scorecard per template: (card, documents, by_kind). The speed is the whole run's (documents per minute
    across every template in it), not a timing per template; by_kind is left empty here, the per-kind table is in the printed report."""
    by_t: dict[str, dict] = defaultdict(lambda: {"fields": 0, "right": 0, "wrong_auto": 0, "queued": 0, "queued_right": 0, "docs": set(), "kinds": {}})
    for (filename, _fid), f in run["fields"].items():
        t = by_t[run["templates"][filename]]
        t["docs"].add(filename)
        t["fields"] += 1
        t["right"] += f["right"]
        t["wrong_auto"] += (f["tier"] == "auto" and not f["right"])
        if f["tier"] != "auto":
            t["queued"] += 1
            t["queued_right"] += f["right"]
    out = {}
    for template_id, t in by_t.items():
        out[template_id] = (selector.Card(run["engine"], corpus, t["fields"], t["right"], t["wrong_auto"], t["queued"], t["queued_right"], round(run["docs_per_min"], 3)),
                            len(t["docs"]), {})
    return out


def save_runs(runs: list[dict], corpus: str, promote: bool) -> None:
    cards_by_template: dict[str, dict[str, selector.Card]] = defaultdict(dict)
    for run in runs:
        for template_id, (card, docs, by_kind) in per_template(run, corpus).items():
            store.save_scorecard(template_id, card, docs, by_kind)
            cards_by_template[template_id][run["engine"]] = card
            print(f"saved: {template_id} / {run['engine']}: {card.right}/{card.fields} right ({100 * card.accuracy:.1f}%), corpus {corpus}")
    for a, b in itertools.combinations(runs, 2):
        for template_id in {t for t in a["templates"].values()} & {t for t in b["templates"].values()}:
            keys = [k for k in a["fields"] if k in b["fields"] and a["templates"][k[0]] == template_id]
            agreed = [k for k in keys if a["fields"][k]["value"].strip() and " ".join(a["fields"][k]["value"].split()).lower() == " ".join(b["fields"][k]["value"].split()).lower()]
            store.save_pair(template_id, selector.Pair(*sorted((a["engine"], b["engine"])), corpus, len(keys), len(agreed), sum(a["fields"][k]["right"] for k in agreed)))
    if not promote:
        return
    for template_id, cards in cards_by_template.items():
        # the incumbents and the baseline were measured now or earlier; only cards on THIS corpus can be compared
        known = {**store.latest_cards(template_id), **cards}
        promoted = store.promoted_engines(template_id)
        for name, card in cards.items():
            verdict = promotion.judge(card, known, promoted)
            print(f"promotion {template_id} / {name}: {'PROMOTED' if verdict.promote else 'not promoted'}: {verdict.reason}")
            if verdict.promote:
                store.promote(template_id, name, corpus, verdict.numbers)
                promoted.add(name)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images-dir", required=True, type=Path, action="append")
    ap.add_argument("--ground-truth", required=True, type=Path, action="append")
    ap.add_argument("--engine", required=True, action="append", help="registry engine name, or `tesseract` (repeatable)")
    ap.add_argument("--limit", type=int, help="score only the first N files of each ground truth (a quick look)")
    ap.add_argument("--out", type=Path, help="write the scorecard as JSON")
    ap.add_argument("--save", action="store_true", help="store each engine's scorecard per template (and the pair statistics) in the database")
    ap.add_argument("--promote", action="store_true", help="with --save: promote the engines that beat the incumbent on this corpus into their template's chain")
    args = ap.parse_args()
    if len(args.images_dir) != len(args.ground_truth):
        ap.error("give one --ground-truth for each --images-dir")

    corpus = [(d, json.loads(g.read_text(encoding="utf-8"))) for d, g in zip(args.images_dir, args.ground_truth)]
    runs = []
    for name in args.engine:
        print(f"running {name} ...", flush=True)
        run = run_engine(name, corpus, args.limit)
        print_engine(run)
        runs.append(run)
    for a, b in itertools.combinations(runs, 2):
        print_pair(a, b)
    if args.save:
        save_runs(runs, corpus_id(corpus, args.limit), args.promote)
    elif args.promote:
        ap.error("--promote needs --save")
    if args.out:
        serial = [{**r, "fields": {f"{k[0]}|{k[1]}": v for k, v in r["fields"].items()}} for r in runs]
        args.out.write_text(json.dumps(serial, indent=1), encoding="utf-8")
        print(f"\nscorecard written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
