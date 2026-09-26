#!/usr/bin/env python
"""
End-to-end validation THROUGH THE BATCH API (tooling only).

evaluate_accuracy.py calls the pipeline functions in-process. This submits the same
corpus to a running API as ONE POST /documents/batch, waits for the workers, and
scores what comes back over HTTP - so it measures the deployed thing: ingestion,
Module 2's page detection and deskew, Module 5 classification, Module 6/7 with the
approval tiers, the queue, the retry machinery and the status endpoint together.

Two differences from evaluate_accuracy.py, both deliberate:
  * No type_hint is sent (unless --type-hint), so Module 5 must classify each file
    the way an unlabelled upload would. A file it does not confirm contributes no
    fields; evaluate_accuracy.py instead measures extraction against the expected
    template even after a classification miss.
  * The approval tier scored is the one the API returned, not one recomputed from a
    threshold - so what is reported is what a client would have seen. (The sweep
    tables still recompute, and this checks the two agree at the configured value.)

Throughput is documents per minute from submitting the batch to its last document
finishing, so it includes queueing and the poll interval. It depends on how many
workers are running and on the machine; run it again after `docker compose up
--scale worker=N` to see the scaling.

Usage:
    python scripts/evaluate_batch.py --api http://localhost:8010 \\
        --images-dir tests/fixtures/synthetic_awb --ground-truth tests/fixtures/synthetic_awb/ground_truth.json \\
        --images-dir tests/fixtures --ground-truth tests/fixtures/ground_truth.json [--sweep]

The template store behind the API must already hold the templates the ground truth
names (docker compose exec app python scripts/seed_templates.py).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

from app.config import CONFIDENCE_AUTO_APPROVE_THRESHOLD  # noqa: E402
from app.modules.validation import has_validator  # noqa: E402
from scripts.evaluate_accuracy import (  # noqa: E402
    OUTCOME_AMBIGUOUS,
    OUTCOME_CORRECT,
    OUTCOME_NEW_TEMPLATE,
    OUTCOME_WRONG,
    TIER_LABELS,
    FieldOutcome,
    FileResult,
    _approval_grid,
    _is_real_file,
    _tier_at,
    _tiers_to_show,
    approval_sweep,
    detect_tier,
    normalize,
)
from scripts.seed_templates import ALL_TEMPLATES  # noqa: E402

TERMINAL_BATCH = ("completed", "completed_with_errors", "failed")

# Wrong values Module 7 accepted in Step 0's field list and the full Step 1 sweep, by (file, field):
# named so the report can say for each one whether it is now caught.
KNOWN_ACCEPTED_WRONG = {
    ("synthetic__photo_keystone.jpg", "shipper_name"): "Step 0",
    ("synthetic__photo_tight_framing.jpg", "flight_date"): "Step 0",
    ("synthetic__photo_wide_framing.jpg", "shipper_name"): "Step 0",
    ("synthetic__photo_rotated.jpg", "shipper_name"): "Step 1 sweep",
    ("synthetic__photo_noisy_lowq.jpg", "declared_value_customs"): "Step 1 sweep",
    ("synthetic__photo_harsh_combined.jpg", "flight_date"): "Step 1 sweep",
    ("synthetic__scan_dark_speckle.pdf", "flight_date"): "Step 1 sweep",
}


def _validated_fields() -> dict[tuple[str, str], bool]:
    """(template_id, field_id) -> the template gives the field a regex or a checksum."""
    return {(t.template_id, f.id): has_validator(f) for t in ALL_TEMPLATES for f in t.fields}


def _to_file_result(filename: str, path: Path, expected: dict, doc: dict, validated: dict) -> FileResult:
    result = FileResult(filename=filename, expected_template=expected["template_id"])
    result.tier = expected.get("tier") or detect_tier(path.read_bytes())

    if doc["status"] != "completed":
        result.error = f"batch document {doc['status']} after {doc['attempts']} attempt(s): {doc['error']}"
        return result

    body = doc["result"]
    if body["page_count"] > 1:
        result.note = f"{body['page_count']} pages - only page 1 evaluated"
    page = body["pages"][0]

    if not page["confirmed"]:
        result.classification = OUTCOME_AMBIGUOUS if page["reason"] == "ambiguous_match" else OUTCOME_NEW_TEMPLATE
        result.extraction_skipped = f"Module 5 did not confirm ({page['reason']}), so the batch produced no fields"
        return result

    compiled = page["result"]
    result.matched_template = compiled["template_id"]
    result.classification = OUTCOME_CORRECT if compiled["template_id"] == result.expected_template else OUTCOME_WRONG
    result.text_native = page["text_native"]
    for field_id, expected_value in expected["fields"].items():
        got = compiled["fields"].get(field_id)
        actual = got["value"] if got else ""
        result.fields.append(
            FieldOutcome(
                field_id=field_id,
                expected=expected_value,
                actual=actual,
                correct=normalize(actual) == normalize(expected_value),
                confidence=got["confidence"] if got else 0.0,
                accepted=bool(got) and not got["needs_review"],
                has_validator=validated.get((compiled["template_id"], field_id), False),
            )
        )
    result.api_tiers = {f: v["approval_tier"] for f, v in compiled["fields"].items()}  # type: ignore[attr-defined]
    return result


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def run(api: str, corpora: list[tuple[Path, dict]], type_hint: str | None, poll: float, timeout: float):
    entries = [(name, images_dir / name, expected) for images_dir, gt in corpora for name, expected in gt.items()]
    missing = [name for name, path, _ in entries if not path.exists()]
    if missing:
        raise SystemExit(f"not found: {missing}")

    files = [("files", (name, path.read_bytes(), "application/octet-stream")) for name, path, _ in entries]
    data = {"type_hint": type_hint} if type_hint else {}
    with httpx.Client(base_url=api, timeout=120) as client:
        started = time.perf_counter()
        resp = client.post("/documents/batch", files=files, data=data)
        resp.raise_for_status()
        submitted = resp.json()
        accepted_in = time.perf_counter() - started
        batch_id = submitted["batch_id"]
        print(f"submitted {len(entries)} documents: HTTP {resp.status_code} in {accepted_in:.2f}s, batch {batch_id}")

        last_line = None
        while True:
            status = client.get(f"/documents/batch/{batch_id}/status").json()
            elapsed = time.perf_counter() - started
            line = f"  {status['status']:22} " + " ".join(f"{k}={v}" for k, v in status["counts"].items() if v)
            if line != last_line:
                print(f"  t={elapsed:6.1f}s{line}", flush=True)
                last_line = line
            if status["status"] in TERMINAL_BATCH:
                break
            if elapsed > timeout:
                raise SystemExit(f"batch still {status['status']} after {timeout:.0f}s")
            time.sleep(poll)
        wall = time.perf_counter() - started

    return entries, status, wall, accepted_in


def report(entries, status, wall, accepted_in, api, show_sweep: bool) -> int:
    validated = _validated_fields()
    docs = status["documents"]
    results = [_to_file_result(name, path, expected, doc, validated) for (name, path, expected), doc in zip(entries, docs)]
    ok = [r for r in results if r.error is None]

    print(f"\n=== Throughput ({api}) ===")
    finished = [d for d in docs if d["status"] == "completed"]
    print(f"  {len(docs)} documents ({len(finished)} completed, {sum(d['status'] == 'failed' for d in docs)} failed), "
          f"submit -> last finished: {wall:.1f}s  =>  {60 * len(docs) / wall:.1f} documents/minute")
    per_doc = [s for d in docs if (s := _seconds(d["started_at"], d["finished_at"])) is not None]
    if per_doc:
        print(f"  per document, worker time: median {statistics.median(per_doc):.1f}s, mean {statistics.mean(per_doc):.1f}s, "
              f"min {min(per_doc):.1f}s, max {max(per_doc):.1f}s (sum {sum(per_doc):.0f}s of worker time in {wall:.1f}s wall)")
        print(f"  => effective parallelism {sum(per_doc) / wall:.2f}x (worker seconds per wall second)")
    retried = [d["filename"] for d in docs if d["attempts"] > 1]
    print(f"  documents that needed a retry: {len(retried)}" + (f" {retried}" if retried else ""))
    for r in results:
        if r.error:
            print(f"  FAILED {r.filename}: {r.error}")

    tiers = _tiers_to_show(results)
    print("\n=== Module 5 classification and field accuracy, by input tier (no type_hint) ===")
    print(f"  {'tier':28} {'files':>5} {'confirmed correctly':>20} {'fields correct':>16}")
    for tier in tiers:
        in_tier = [r for r in ok if r.tier == tier]
        if not in_tier:
            print(f"  {TIER_LABELS[tier]:28} {0:>5}")
            continue
        confirmed = sum(r.classification == OUTCOME_CORRECT for r in in_tier)
        fields = [f for r in in_tier for f in r.fields]
        print(f"  {TIER_LABELS[tier]:28} {len(in_tier):>5} {confirmed:>13}/{len(in_tier):<6} "
              f"{sum(f.correct for f in fields):>9}/{len(fields):<6}")
    for r in ok:
        if r.classification != OUTCOME_CORRECT:
            print(f"  NOT CLASSIFIED CORRECTLY: {r.filename}: {r.classification} - {r.extraction_skipped}")

    print(f"\n=== Approval tiers AS RETURNED BY THE API (configured auto threshold {CONFIDENCE_AUTO_APPROVE_THRESHOLD:.2f}) ===")
    print(f"  {'tier':28} {'fields':>6} {'auto':>10} {'spot_check':>11} {'review':>7} {'auto precision':>18}")
    mismatches = 0
    for tier in tiers:
        outs = [(r, f) for r in ok if r.tier == tier for f in r.fields]
        if not outs:
            continue
        counts = {"auto": 0, "spot_check": 0, "review": 0}
        auto_correct = 0
        for r, f in outs:
            api_tier = r.api_tiers.get(f.field_id)  # type: ignore[attr-defined]
            counts[api_tier] += 1
            auto_correct += api_tier == "auto" and f.correct
            mismatches += _tier_at(f, CONFIDENCE_AUTO_APPROVE_THRESHOLD) != api_tier
        n = len(outs)
        auto, spot, review = counts["auto"], counts["spot_check"], counts["review"]
        precision = f"{auto_correct}/{auto} = {100 * auto_correct / auto:5.1f}%" if auto else "n/a"
        auto_cell = f"{auto} ({100 * auto / n:.0f}%)"
        print(f"  {TIER_LABELS[tier]:28} {n:>6} {auto_cell:>10} {f'{100 * spot / n:.0f}%':>11} "
              f"{f'{100 * review / n:.0f}%':>7} {precision:>18}")
    print(f"  (the API's tier disagreed with a recomputation from acceptance, validator and confidence in {mismatches} field(s))")

    print("\n=== Wrong values Module 7 accepted, and where each landed ===")
    slipping = 0
    seen_known = set()
    for r in ok:
        for f in r.fields:
            if f.accepted and not f.correct:
                api_tier = r.api_tiers[f.field_id]  # type: ignore[attr-defined]
                origin = KNOWN_ACCEPTED_WRONG.get((r.filename, f.field_id))
                seen_known.add((r.filename, f.field_id))
                verdict = "SLIPPING THROUGH AS AUTO" if api_tier == "auto" else f"caught ({api_tier})"
                slipping += api_tier == "auto"
                kind = "real" if _is_real_file(r.filename) else "synthetic"
                print(f"  {verdict:26} conf {f.confidence:5.1f}  {r.filename[-32:]:32} {f.field_id:24} read {f.actual[:30]!r} "
                      f"expected {f.expected[:24]!r} [{kind}{', ' + origin if origin else ', NEW: not in any earlier list'}]")
    absent = [k for k in KNOWN_ACCEPTED_WRONG if k not in seen_known]
    for filename, field in absent:
        print(f"  (earlier accepted-wrong case no longer occurs: {filename} / {field} - now read correctly, or rejected by Module 7)")
    print(f"  => {slipping} wrong value(s) auto-approved" if slipping else "  => no wrong value was auto-approved")

    if show_sweep:
        print()
        approval_sweep(results, _approval_grid(0.5, 1.0, 0.05))
    return 1 if any(r.error for r in results) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://localhost:8000", help="base URL of a running API")
    parser.add_argument("--images-dir", required=True, type=Path, action="append")
    parser.add_argument("--ground-truth", required=True, type=Path, action="append")
    parser.add_argument("--type-hint", default=None, help="send a type_hint with the batch (default: none - Module 5 classifies)")
    parser.add_argument("--poll", type=float, default=1.0, help="seconds between status polls")
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--sweep", action="store_true", help="also print the per-tier approval-threshold sweep")
    args = parser.parse_args()
    if len(args.images_dir) != len(args.ground_truth):
        parser.error("give one --ground-truth for each --images-dir")

    corpora = [(d, json.loads(g.read_text(encoding="utf-8"))) for d, g in zip(args.images_dir, args.ground_truth)]
    entries, status, wall, accepted_in = run(args.api, corpora, args.type_hint, args.poll, args.timeout)
    return report(entries, status, wall, accepted_in, args.api, args.sweep)


if __name__ == "__main__":
    sys.exit(main())
