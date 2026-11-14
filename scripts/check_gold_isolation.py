#!/usr/bin/env python
"""
Fails (exit 1) if any gold real waybill, its values or anything derived from its layout has reached an input of the pipeline:
templates and their seed, landmark lists, synthetic-document generators, format pools, generated corpora and their ground truth.

    python scripts/check_gold_isolation.py

What it checks (data/gold_real/SPLIT.json says which documents are gold):
  1. the gold files and labels are unchanged (SHA-256) and read-only;
  2. every committed landmark list (scripts/landmarks/*.json other than the vision reader's frozen *_trainonly files) has a PROVENANCE.json entry whose
     source documents are all dev-pool documents, and the entry's hashes are those of the files on disk;
  3. no generator or template input names a gold file, or uses one as a --base / BASE (resolved by hash);
  4. no gold label VALUE that identifies a shipment (AWB number, shipper, consignee and agent names and addresses, agent IATA code, goods, HS code,
     invoice reference, accounting information) of eight or more letters and digits occurs in any template, landmark list, generator, reader
     profile or synthetic ground truth, unless a dev-pool label carries the same value (then it is the layout's or the airline's own boilerplate,
     not this document's). Values are compared with everything but letters and digits removed. Airports, airlines, currencies, dates and
     charges are public vocabulary that every generator legitimately holds and are not searched.

It cannot see a human choosing a threshold while looking at a gold page; that is what "aggregate only" scoring is for.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import stat
import sys
from collections import Counter
from pathlib import Path

from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent
GOLD = ROOT / "data" / "gold_real"

# Code and data that build or shape what the pipeline reads or what it is measured on. The vision reader's research tooling is out of scope
# (the loop does not use a trained adapter) and is listed in SPLIT.json's "exposure_disclosed".
CODE_INPUTS = [
    "scripts/seed_templates.py", "scripts/waybill_forms.py", "scripts/synth_formats.py", "scripts/synth_bkk.py", "scripts/drift.py",
    "scripts/generate_synthetic_awb.py", "scripts/build_demo_batch.py", "scripts/build_synthetic_corpus.py", "scripts/build_landmarks.py",
    "scripts/make_gold_split.py", "scripts/prune_dev_templates.py",
]
DATA_GLOBS = ["scripts/landmarks/*.json", "data/reader_profiles/*.json", "data/demo_50/ground_truth.json", "data/holdout_50/ground_truth.json",
              "tests/fixtures/ground_truth.json"]  # the corpora the loop measures on; data/synthetic_* belong to the vision adapter's training and validation
IDENTITY_FIELDS = ("awb_number", "shipper_name", "shipper_address", "consignee_name", "consignee_address", "agent_name", "agent_iata_code",
                   "accounting_information", "nature_of_goods", "hs_code", "invoice_reference")
MIN_VALUE_CHARS = 8
FROZEN_REPLAY_LANDMARKS = {"air_waybill_trainonly.json", "air_waybill_bkk_trainonly.json"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def tk(s: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 3]


def strings_of(rel: str, text: str) -> list[str]:
    """Every string a generator, template seed, landmark list, reader profile or ground truth holds: string constants of Python source, leaves of JSON."""
    if rel.endswith(".py"):
        return [n.value for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) >= 4]
    out: list[str] = []

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            for k, v in x.items():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    try:
        walk(json.loads(text))
    except ValueError:
        pass
    return out


def fuzzy_leaks(gold_labels: dict[str, str], dev_tokens: set[str], texts: dict[str, str]):
    """FUZZY, token-level. A gold label string is a candidate against an input when, for one input string, at least 60% of its tokens (three or
    more letters or digits; a token matches an input token when equal or, from five characters, 90% similar) are found in it, or when its letters
    and digits are a substring of it or contain it (eight characters or more). A candidate is a FAILURE only if it is an identity field AND the
    tokens the dev pool's labels do not also carry (the gold string's own distinctive content: a name, an address, a serial) are themselves
    found in the input at 60% or more; otherwise it is a NOTE (public vocabulary, or printed boilerplate every document of the form carries).
    Returns (failures, notes as {input: count})."""
    fails: list[tuple[str, str, float]] = []
    notes: Counter = Counter()
    for rel, text in texts.items():
        strs = strings_of(rel, text)
        tsets = [set(tk(x)) for x in strs]
        vocab = sorted(set().union(*tsets)) if tsets else []
        sq = [squash(x) for x in strs]
        for key, g in gold_labels.items():
            field = key.split(":", 1)[1]
            gt = tk(g)
            if not gt:
                continue

            def found(t: str, ts: set[str]) -> bool:
                return t in ts or (len(t) >= 5 and any(fuzz.ratio(t, u) >= 90 for u in ts))

            best = max((sum(found(t, ts) for t in gt) / len(gt) for ts in tsets), default=0.0)
            gs = squash(g)
            substring = len(gs) >= 8 and any(gs in x or (len(x) >= 10 and x in gs) for x in sq)
            if best < 0.6 and not substring:
                continue
            unique = [t for t in gt if t not in dev_tokens]
            if field in IDENTITY_FIELDS and unique and (len(unique) >= 2 or len(unique[0]) >= 6):
                u_best = max((sum(found(t, ts) for t in unique) / len(unique) for ts in tsets), default=0.0)
                if u_best >= 0.6:
                    fails.append((rel, field, round(u_best, 2)))
                    continue
            notes[rel] += 1
    return fails, notes


def main() -> int:
    split = json.loads((GOLD / "SPLIT.json").read_text(encoding="utf-8"))
    gold = split["documents"]
    problems: list[str] = []

    # 1. the gold files are what was sealed, and read-only
    for d in gold:
        for rel, want in ((d["file"], d["sha256"]), (f"labels/{d['label_file']}", d["label_sha256"])):
            p = GOLD / rel
            if not p.exists() or sha(p) != want:
                problems.append(f"gold file changed or missing: {rel}")
            elif os.stat(p).st_mode & stat.S_IWRITE:
                problems.append(f"gold file is writable: {rel}")
    gold_hashes = {d["sha256"] for d in gold}
    gold_names = {d["file"] for d in gold}

    # 2. landmark provenance
    prov = json.loads((ROOT / "scripts" / "landmarks" / "PROVENANCE.json").read_text(encoding="utf-8"))
    for lm in sorted((ROOT / "scripts" / "landmarks").glob("*.json")):
        if lm.name == "PROVENANCE.json" or lm.name in FROZEN_REPLAY_LANDMARKS:
            continue
        entry = prov.get(lm.name)
        if not entry:
            problems.append(f"landmark list without provenance: {lm.name}")
            continue
        for s in entry["sources"]:
            if s["sha256"] in gold_hashes or s["file"] in gold_names:
                problems.append(f"{lm.name} was built from a gold document: {s['file']}")

    # 3. generator bases and names
    texts: dict[str, str] = {}
    for rel in CODE_INPUTS:
        p = ROOT / rel
        if p.exists():
            texts[rel] = p.read_text(encoding="utf-8", errors="replace")
    for pattern in DATA_GLOBS:
        for p in ROOT.glob(pattern):
            if p.name in FROZEN_REPLAY_LANDMARKS:
                continue
            texts[str(p.relative_to(ROOT))] = p.read_text(encoding="utf-8", errors="replace")
    for rel, text in texts.items():
        for name in gold_names:
            if name in text or name.split("AWBUpload")[0][:11] in text and re.search(r"\d{11}", name):
                if name in text or name.split("AWBUpload")[0][:11] in text:
                    problems.append(f"{rel} names the gold document {name}")
    for base in list((ROOT / "tests" / "fixtures").glob("*.pdf")) + list((ROOT / "data" / "real_awb").glob("*/*.pdf")):
        if sha(base) in gold_hashes and any(base.name in t for t in texts.values()):
            problems.append(f"a generator input refers to gold file {base.name}")

    # 4. gold values in any input
    dev_values: set[str] = set()
    for lp in (ROOT / "data" / "real_awb" / "labels").glob("*.json"):
        if lp.name in {d["label_file"] for d in gold}:
            continue
        dev_values |= {squash(str(v)) for k, v in json.loads(lp.read_text(encoding="utf-8"))["fields"].items() if k in IDENTITY_FIELDS}
    values: set[str] = set()
    for d in gold:
        label = json.loads((GOLD / "labels" / d["label_file"]).read_text(encoding="utf-8"))
        for k, v in label["fields"].items():
            q = squash(str(v))
            if k in IDENTITY_FIELDS and len(q) >= MIN_VALUE_CHARS and q not in dev_values:
                values.add(q)
    squashed = {rel: squash(t) for rel, t in texts.items()}
    for v in sorted(values):
        for rel, t in squashed.items():
            if v in t:
                problems.append(f"{rel} contains a gold identity value ({len(v)} characters)")

    # 5. the fuzzy pass over every generator, seed, landmark list, reader profile and ground truth
    labels = {}
    for d in gold:
        lab = json.loads((GOLD / "labels" / d["label_file"]).read_text(encoding="utf-8"))
        for k, v in lab["fields"].items():
            if str(v).strip():
                labels[f"{d['id']}:{k}"] = str(v)
    dev_tokens = {t for lp in (ROOT / "data" / "real_awb" / "labels").glob("*.json") if lp.name not in {d["label_file"] for d in gold}
                  for v in json.loads(lp.read_text(encoding="utf-8"))["fields"].values() for t in tk(str(v))}
    fails, notes = fuzzy_leaks(labels, dev_tokens, texts)
    for rel, field, ov in fails:
        problems.append(f"FUZZY {rel}: the distinctive tokens of a gold {field} string are in one of its strings ({ov:.0%})")
    print(f"fuzzy pass: {len(labels)} gold strings against {len(texts)} inputs; {len(fails)} identity-field leaks; "
          f"{sum(notes.values())} notes (public vocabulary or boilerplate the dev pool carries too) in {len(notes)} inputs")

    if problems:
        print("GOLD ISOLATION FAILED")
        for p in sorted(set(problems)):
            print("  -", p)
        return 1
    print(f"gold isolation ok: {len(gold)} gold documents, {len(values)} distinct label values searched in {len(texts)} inputs, "
          f"{sum(1 for l in (ROOT / 'scripts' / 'landmarks').glob('*.json') if l.name not in FROZEN_REPLAY_LANDMARKS | {'PROVENANCE.json'})} landmark lists with provenance")
    return 0


if __name__ == "__main__":
    sys.exit(main())
