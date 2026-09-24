#!/usr/bin/env python
"""
Accuracy evaluation harness (tooling only).

Per the reference doc's testing strategy: measure per-field accuracy against
hand-verified ground truth, then tune each field's confidence_floor until the
false-accept rate is ~0. This script does the measuring; tuning based on its
output is a separate, human judgment call, not automated here.

Two questions are measured separately, because they fail for different reasons
and are fixed in different places:

  CLASSIFICATION - given no type_hint, does Module 5 pick the right template,
    or land on a wrong one / ambiguous_match / new_template_needed?
  EXTRACTION - given the right template, are the field values right?

Field accuracy is reported per INPUT TIER rather than as one blended number,
since the tiers behave very differently:
  text-layer PDF        a PDF whose pages carry embedded text (PyMuPDF get_text())
  scanned / flat PDF    a PDF with no text layer - an image in a PDF wrapper
  camera-photo JPEG     a JPEG, i.e. something a camera produced
The tier is detected per file. A PNG (or any other image) fits none of these
and is reported as its own "other image" row rather than blended into one.

This runs the real pipeline path: Module 1 ingestion (so PDFs work), generic
preprocessing, Module 5, then pipeline.extract_page_fields - which reads a
text-native PDF page straight from its embedded text and sends every other
page through the template's second-pass preprocessing and OCR. The per-file
line says which path each file took. It does not write to the database.

Usage:
    python scripts/evaluate_accuracy.py --images-dir DIR --ground-truth FILE.json
    python scripts/evaluate_accuracy.py --images-dir DIR --ground-truth FILE.json --sweep

    # several corpora in one run (each --images-dir pairs with the --ground-truth at the same position)
    python scripts/evaluate_accuracy.py --images-dir A --ground-truth A/gt.json --images-dir B --ground-truth B/gt.json --sweep

--sweep prints the approval-tier sweep (per input tier: what fraction of fields land in
auto / spot_check / review at each CONFIDENCE_AUTO_APPROVE_THRESHOLD, and how precise the
auto-approved ones are) and then the per-field confidence_floor false-accept table.

Ground truth JSON shape (paths are relative to --images-dir):
    {
      "card.jpg": {
        "template_id": "bits_goa_id_card",
        "version": 1,                 // optional - defaults to the version anchors confirmed
        "tier": "camera_photo_jpeg",  // optional - overrides detection (see below)
        "fields": {"student_name": "John Doe", "id_number": "2024A7PS0476G"}
      },
      ...
    }

Only the FIRST page of a multi-page file is evaluated.

Tier detection caveat: "has a text layer" is not the same as "is a digital-born
PDF". A scan that has been through OCR carries an invisible text layer and will
be detected as a text-layer PDF here. The pipeline itself is not fooled - it
refuses to treat invisible text as native and reads such a page through OCR -
so the per-file "read via" line can disagree with the tier for those files.
Set "tier" in the ground truth to put them where they belong.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import fitz  # noqa: E402
import magic  # noqa: E402

from app.runtime import configure_tesseract  # noqa: E402
from app.modules.ingestion import extract_text_layers, ingest_file  # noqa: E402
from app.modules.pipeline import extract_page_fields  # noqa: E402
from app.modules.preprocessing import preprocess_image  # noqa: E402
from app.modules.template_store import get_latest_template, get_template_version, list_known_doc_types  # noqa: E402
from app.config import CONFIDENCE_AUTO_APPROVE_THRESHOLD  # noqa: E402
from app.modules.type_identification import identify_document_type  # noqa: E402
from app.modules.validation import validate_fields  # noqa: E402

configure_tesseract()

TIER_TEXT_PDF = "text_layer_pdf"
TIER_SCANNED_PDF = "scanned_pdf"
TIER_CAMERA_JPEG = "camera_photo_jpeg"
TIER_OTHER_IMAGE = "other_image"

TIER_LABELS = {
    TIER_TEXT_PDF: "text-layer PDF",
    TIER_SCANNED_PDF: "scanned / flat-image PDF",
    TIER_CAMERA_JPEG: "camera-photo JPEG",
    TIER_OTHER_IMAGE: "other image (PNG etc.)",
}
CORE_TIERS = [TIER_TEXT_PDF, TIER_SCANNED_PDF, TIER_CAMERA_JPEG]  # always reported, even with no samples

OUTCOME_CORRECT = "correct"
OUTCOME_WRONG = "wrong_template"
OUTCOME_AMBIGUOUS = "ambiguous_match"
OUTCOME_NEW_TEMPLATE = "new_template_needed"
OUTCOMES = [OUTCOME_CORRECT, OUTCOME_WRONG, OUTCOME_AMBIGUOUS, OUTCOME_NEW_TEMPLATE]


def normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def detect_tier(data: bytes) -> str:
    """Classifies one input file by what it actually is, not by its extension
    (the same content-sniffing Module 1 applies): a PDF with any non-empty
    get_text() is a text-layer PDF, a PDF without is a scan, a JPEG is a
    camera photo, and any other image is reported separately."""
    mime = magic.from_buffer(data, mime=True)
    if mime == "application/pdf":
        doc = fitz.open(stream=data, filetype="pdf")
        try:
            has_text = any(page.get_text().strip() for page in doc)
        finally:
            doc.close()
        return TIER_TEXT_PDF if has_text else TIER_SCANNED_PDF
    if mime == "image/jpeg":
        return TIER_CAMERA_JPEG
    return TIER_OTHER_IMAGE


@dataclass
class FieldOutcome:
    field_id: str
    expected: str
    actual: str
    correct: bool
    confidence: float
    accepted: bool = True  # Module 7 accepted it (regex, checksum and confidence floor all passed)
    has_validator: bool = False  # the template gives it a regex or a checksum, i.e. more than confidence to judge it by


@dataclass
class FileResult:
    filename: str
    tier: str = ""
    expected_template: str = ""
    classification: Optional[str] = None  # one of OUTCOMES
    matched_template: Optional[str] = None  # what Module 5 confirmed, if it confirmed anything
    anchor_detail: list[tuple[str, float, float]] = field(default_factory=list)  # (text, score, threshold) for the EXPECTED template
    fields: list[FieldOutcome] = field(default_factory=list)  # empty when extraction did not run
    extraction_skipped: Optional[str] = None  # why extraction could not run
    error: Optional[str] = None  # the file could not be evaluated at all
    note: Optional[str] = None
    text_native: bool = False  # fields were read from the embedded text layer, with no OCR


def _classify(identification, expected_template: str) -> str:
    if identification.confirmed:
        return OUTCOME_CORRECT if identification.template_id == expected_template else OUTCOME_WRONG
    if identification.reason == "ambiguous_match":
        return OUTCOME_AMBIGUOUS
    return OUTCOME_NEW_TEMPLATE  # no_match: Module 5 rejects, pipeline routes it to new_template_needed


def evaluate_file(path: Path, filename: str, expected: dict) -> FileResult:
    """Runs one file through the real pipeline path and scores it.

    Classification and extraction are deliberately independent: classification
    runs with NO type_hint (what an unlabelled upload gets), while extraction
    is scored against the ground-truth template whatever Module 5 decided, so
    a classification failure never hides how well the fields would have read.
    Extraction only stays skipped when even the hinted anchors fail, since the
    pipeline would refuse to extract there too."""
    result = FileResult(filename=filename, expected_template=expected["template_id"])
    if not path.exists():
        result.error = f"file not found at {path}"
        return result

    data = path.read_bytes()
    result.tier = expected.get("tier") or detect_tier(data)

    pages, metadata = ingest_file(data, filename=filename, type_hint=None)
    if metadata.page_count > 1:
        result.note = f"{metadata.page_count} pages - only page 1 evaluated"
    page = pages[0]
    generic = preprocess_image(page)

    identification = identify_document_type(generic.image_bytes, type_hint=None)
    result.classification = _classify(identification, result.expected_template)
    if identification.confirmed:
        result.matched_template = identification.template_id

    expected_template = get_latest_template(result.expected_template)
    if expected_template is not None:
        scores = identification.per_anchor_scores.get(result.expected_template, {})
        result.anchor_detail = [
            (anchor.text, scores[anchor.text], anchor.match_threshold * 100)
            for anchor in expected_template.anchors
            if anchor.text in scores
        ]

    if result.classification == OUTCOME_CORRECT:
        confirmed_version = identification.version
    else:
        hinted = identify_document_type(generic.image_bytes, type_hint=result.expected_template)
        if not hinted.confirmed:
            result.extraction_skipped = (
                f"anchor verification failed even with the correct type_hint ({hinted.reason}) - "
                "fix the template or the input before measuring field accuracy"
            )
            return result
        confirmed_version = hinted.version

    version = expected.get("version") or confirmed_version
    template = get_template_version(result.expected_template, version)
    if template is None:
        result.error = f"no template {result.expected_template!r} version {version}"
        return result

    text_layer = None
    if metadata.text_native_pages and metadata.text_native_pages[0]:
        text_layer = extract_text_layers(data)[0]
    extraction = extract_page_fields(template, page, generic, text_layer)
    extractions = extraction.fields
    result.text_native = extraction.text_native

    summary = validate_fields(template, extractions)
    validated = {f.id for f in template.fields if f.validation_regex is not None or f.checksum is not None}
    for field_id, expected_value in expected["fields"].items():
        actual = extractions.get(field_id)
        actual_text = actual.raw_text if actual else ""
        result.fields.append(
            FieldOutcome(
                field_id=field_id,
                expected=expected_value,
                actual=actual_text,
                correct=normalize(actual_text) == normalize(expected_value),
                confidence=actual.aggregate_confidence if actual else 0.0,
                accepted=field_id in summary.accepted,
                has_validator=field_id in validated,
            )
        )
    return result


def evaluate_all(images_dir: Path, ground_truth: dict) -> list[FileResult]:
    results = []
    for filename, expected in ground_truth.items():
        try:
            results.append(evaluate_file(images_dir / filename, filename, expected))
        except Exception as exc:
            failed = FileResult(filename=filename, expected_template=expected.get("template_id", ""))
            failed.error = f"{type(exc).__name__}: {exc}"
            results.append(failed)
    return results


def _pct(correct: int, total: int) -> str:
    return f"{100 * correct / total:.1f}%" if total else "-"


def _tiers_to_show(results: list[FileResult]) -> list[str]:
    extra = [t for t in (TIER_OTHER_IMAGE,) if any(r.tier == t for r in results)]
    return CORE_TIERS + extra


def accuracy_report(results: list[FileResult]) -> int:
    evaluated = [r for r in results if r.error is None]
    tiers = _tiers_to_show(results)

    print("=== Field accuracy by input tier ===")
    print(f"  {'tier':28} {'files':>5} {'skipped':>8} {'fields correct':>15} {'accuracy':>9}")
    total_correct = total_fields = 0
    for tier in tiers:
        in_tier = [r for r in evaluated if r.tier == tier]
        ran = [r for r in in_tier if r.extraction_skipped is None]
        correct = sum(f.correct for r in ran for f in r.fields)
        total = sum(len(r.fields) for r in ran)
        total_correct += correct
        total_fields += total
        if not in_tier:
            print(f"  {TIER_LABELS[tier]:28} {0:>5} {'':>8} {'-':>15} {'-':>9}   (no samples in this corpus)")
        else:
            print(
                f"  {TIER_LABELS[tier]:28} {len(in_tier):>5} {len(in_tier) - len(ran):>8} "
                f"{f'{correct}/{total}':>15} {_pct(correct, total):>9}"
            )
    print(
        f"  {'OVERALL':28} {len(evaluated):>5} {sum(r.extraction_skipped is not None for r in evaluated):>8} "
        f"{f'{total_correct}/{total_fields}':>15} {_pct(total_correct, total_fields):>9}"
    )
    print("  ('skipped' = extraction could not run, so those files contribute no fields to the accuracy)")

    print()
    print(f"=== Module 5 classification - no type_hint, scanning {len(list_known_doc_types())} unarchived template(s) ===")
    print(f"  {'tier':28} {'files':>5} {'correct':>8} {'wrong tmpl':>11} {'ambiguous':>10} {'new_tmpl_needed':>16}")
    for tier in tiers:
        in_tier = [r for r in evaluated if r.tier == tier]
        if not in_tier:
            print(f"  {TIER_LABELS[tier]:28} {0:>5} {'-':>8} {'-':>11} {'-':>10} {'-':>16}")
            continue
        counts = {o: sum(r.classification == o for r in in_tier) for o in OUTCOMES}
        print(
            f"  {TIER_LABELS[tier]:28} {len(in_tier):>5} {counts[OUTCOME_CORRECT]:>8} {counts[OUTCOME_WRONG]:>11} "
            f"{counts[OUTCOME_AMBIGUOUS]:>10} {counts[OUTCOME_NEW_TEMPLATE]:>16}"
        )
    correct_total = sum(r.classification == OUTCOME_CORRECT for r in evaluated)
    print(f"  {'OVERALL':28} {len(evaluated):>5} {correct_total:>8}  ({_pct(correct_total, len(evaluated))} classified correctly)")

    print()
    print("=== Per file ===")
    for r in results:
        if r.error:
            print(f"  {r.filename}: ERROR - {r.error}")
            continue
        ran = "extraction skipped" if r.extraction_skipped else f"{sum(f.correct for f in r.fields)}/{len(r.fields)} fields"
        read_via = "" if r.extraction_skipped else ("  |  read via: text layer, no OCR" if r.text_native else "  |  read via: OCR")
        print(f"  {r.filename}  [{TIER_LABELS.get(r.tier, r.tier)}]  module 5: {r.classification}  |  {ran}{read_via}")
        if r.classification != OUTCOME_CORRECT:
            if r.matched_template:
                print(f"      Module 5 confirmed {r.matched_template!r}, expected {r.expected_template!r}")
            for text, score, threshold in r.anchor_detail:
                print(f"      anchor {text[:48]!r}: score {score:.1f} vs threshold {threshold:.0f}")
        if r.note:
            print(f"      note: {r.note}")

    per_field_correct: dict[str, int] = {}
    per_field_total: dict[str, int] = {}
    for r in evaluated:
        for f in r.fields:
            per_field_total[f.field_id] = per_field_total.get(f.field_id, 0) + 1
            per_field_correct[f.field_id] = per_field_correct.get(f.field_id, 0) + f.correct
    if per_field_total:
        print("\n=== Per-field accuracy (all tiers) ===")
        for field_id in sorted(per_field_total):
            c, t = per_field_correct[field_id], per_field_total[field_id]
            print(f"  {field_id}: {c}/{t} ({100 * c / t:.1f}%)")

    mismatches = [(r, f) for r in evaluated for f in r.fields if not f.correct]
    if mismatches:
        print("\n=== Mismatches ===")
        for r, f in mismatches:
            print(
                f"  [{TIER_LABELS.get(r.tier, r.tier)}] {r.filename} / {f.field_id}: "
                f"expected {f.expected!r}, got {f.actual!r} (confidence={f.confidence:.1f})"
            )

    errors = [f"{r.filename}: {r.error}" for r in results if r.error]
    errors += [f"{r.filename}: {r.extraction_skipped}" for r in evaluated if r.extraction_skipped]
    if errors:
        print("\n=== Errors / skipped ===")
        for e in errors:
            print(f"  {e}")

    notes = []
    if any(r.tier == TIER_TEXT_PDF for r in evaluated):
        notes.append('text-layer detection is "any page has text" - an OCR\'d scan also has one; set "tier" in the ground truth to override')
    thin = [TIER_LABELS[t] for t in tiers if 0 < sum(r.tier == t for r in evaluated) < 5]
    if thin:
        notes.append(f"fewer than 5 files in: {', '.join(thin)} - treat those percentages as anecdotes, not rates")
    if notes:
        print("\n=== Notes ===")
        for n in notes:
            print(f"  {n}")

    return 1 if errors else 0


def _is_real_file(filename: str) -> bool:
    """The synthetic corpus names its files reference__* / synthetic__*; everything else is a real document."""
    return not filename.startswith(("reference__", "synthetic__"))


def _approval_grid(start: float, stop: float, step: float) -> list[float]:
    """The regular sweep grid plus a 0.01 grid over 0.90-1.00, where the decision is actually made."""
    grid, t = set(), start
    while t <= stop + 1e-9:
        grid.add(round(t, 2))
        t += step
    grid.update(round(0.90 + 0.01 * i, 2) for i in range(11))
    return sorted(g for g in grid if start - 1e-9 <= g <= stop + 1e-9)


def _tier_at(f: FieldOutcome, threshold: float) -> str:
    """The approval tier Module 7 would give `f` at `threshold` (0-1 scale) - the same rule as
    validation.approval_tier, parameterised so the sweep can vary it."""
    if not f.accepted:
        return "review"
    return "auto" if f.confidence >= threshold * 100 else "spot_check"


def _tier_counts(outs: list[tuple[str, FieldOutcome]], threshold: float) -> tuple[int, int, int, int]:
    """(auto, auto_correct, spot_check, review) at `threshold`."""
    auto = auto_correct = spot = review = 0
    for _, f in outs:
        tier = _tier_at(f, threshold)
        if tier == "auto":
            auto += 1
            auto_correct += f.correct
        elif tier == "spot_check":
            spot += 1
        else:
            review += 1
    return auto, auto_correct, spot, review


def approval_sweep(results: list[FileResult], thresholds: list[float], target_precision: float = 0.99) -> int:
    """
    Per input tier: for each candidate CONFIDENCE_AUTO_APPROVE_THRESHOLD, what fraction
    of fields land in each approval tier and how precise the auto-approved ones are.

    A field is `auto` when Module 7 accepted it and its confidence is at or above the
    threshold, `spot_check` when accepted but under it, `review` when Module 7 rejected
    it (independent of the threshold). Precision is correct / auto; it says nothing
    about the fields that were not auto-approved.

    "Clears the target" means precision >= target at that threshold AND at every higher
    one in the grid: on a corpus this small precision is not monotonic, and a threshold
    that clears only by luck would be a poor thing to ship.
    """
    evaluated = [r for r in results if r.error is None and r.extraction_skipped is None]
    tiers = _tiers_to_show(results)
    exit_code = 0

    for tier in tiers:
        outs = [(r.filename, f) for r in evaluated if r.tier == tier for f in r.fields]
        label = TIER_LABELS[tier]
        print(f"=== Approval-tier sweep: {label} ===")
        if not outs:
            print("  no samples in this corpus\n")
            continue

        files = sum(1 for r in evaluated if r.tier == tier)
        distinct = len({(f.field_id, normalize(f.expected)) for _, f in outs})
        real = sum(1 for name, _ in outs if _is_real_file(name))
        print(f"  {len(outs)} field reads from {files} file(s) ({real} from real documents); "
              f"{distinct} DISTINCT expected values - the reads are not independent samples")
        print(f"  {'threshold':>9} | {'auto':>12} | {'auto precision':>18} | {'wrong in auto':>13} | {'spot_check':>10} | {'review':>7}")

        rows = []
        for t in thresholds:
            auto, auto_ok, spot, review = _tier_counts(outs, t)
            rows.append((t, auto, auto_ok, spot, review))
            precision = f"{auto_ok}/{auto} = {100 * auto_ok / auto:5.1f}%" if auto else "n/a"
            marker = "  <- configured" if abs(t - CONFIDENCE_AUTO_APPROVE_THRESHOLD) < 1e-9 else ""
            n = len(outs)
            print(f"  {t:>9.2f} | {f'{auto} ({100 * auto / n:.0f}%)':>12} | {precision:>18} | {auto - auto_ok:>13} | "
                  f"{f'{100 * spot / n:.0f}%':>10} | {f'{100 * review / n:.0f}%':>7}{marker}")

        # Lowest threshold from which precision stays at or above the target.
        clearing = None
        for i in range(len(rows) - 1, -1, -1):
            t, auto, auto_ok, _, _ = rows[i]
            if auto and auto_ok / auto < target_precision:
                break
            clearing = rows[i] if auto else clearing
        if clearing is None:
            print(f"  -> no threshold on the grid gives auto precision >= {100 * target_precision:.0f}%")
        else:
            t, auto, auto_ok, _, _ = clearing
            print(f"  -> lowest threshold that stays >= {100 * target_precision:.0f}% from there up: {t:.2f} "
                  f"(auto = {auto} of {len(outs)} fields, {100 * auto / len(outs):.0f}%)")

        # The wrong values Module 7 ACCEPTED: these are the ones that could ship silently.
        wrong_accepted = sorted(
            ((name, f) for name, f in outs if f.accepted and not f.correct), key=lambda item: -item[1].confidence
        )
        if wrong_accepted:
            print(f"  wrong values Module 7 accepted ({len(wrong_accepted)}); each is `auto` at every threshold <= its confidence:")
            for name, f in wrong_accepted:
                kind = "real" if _is_real_file(name) else "synthetic"
                checked = "regex/checksum" if f.has_validator else "confidence only"
                print(f"     conf {f.confidence:5.1f}  {f.field_id:24} read {f.actual[:34]!r:38} expected {f.expected[:30]!r:32} [{kind}, {checked}] {name[-34:]}")
        else:
            print("  no wrong value was accepted by Module 7")

        # Same auto-approved set, split by whether anything other than confidence could have judged it.
        t0 = CONFIDENCE_AUTO_APPROVE_THRESHOLD
        auto_outs = [f for _, f in outs if f.accepted and f.confidence >= t0 * 100]
        for validated, name in ((True, "with a regex/checksum"), (False, "confidence only  ")):
            group = [f for f in auto_outs if f.has_validator is validated]
            if group:
                ok = sum(f.correct for f in group)
                print(f"  at the configured {t0:.2f}: auto fields {name} {ok}/{len(group)} correct ({100 * ok / len(group):.1f}%)")
        print()

    errors = [f"{r.filename}: {r.error or r.extraction_skipped}" for r in results if r.error or r.extraction_skipped]
    if errors:
        print("=== Errors / skipped ===")
        for e in errors:
            print(f"  {e}")
        exit_code = 1
    return exit_code


def confidence_sweep(results: list[FileResult], thresholds: list[float]) -> int:
    per_field_samples: dict[str, list[tuple[bool, float]]] = {}
    for r in results:
        for f in r.fields:
            per_field_samples.setdefault(f.field_id, []).append((f.correct, f.confidence))

    field_ids = sorted(per_field_samples)
    print("=== Confidence floor sweep (false-accept rate per threshold) ===")
    print("  threshold | " + " | ".join(field_ids))
    for threshold in thresholds:
        row = [f"{threshold:.2f}"]
        for field_id in field_ids:
            accepted = [correct for correct, conf in per_field_samples[field_id] if conf >= threshold * 100]
            false_accepts = sum(1 for correct in accepted if not correct)
            row.append(f"{false_accepts / len(accepted):.2f}" if accepted else "n/a")
        print("  " + " | ".join(row))

    errors = [f"{r.filename}: {r.error or r.extraction_skipped}" for r in results if r.error or r.extraction_skipped]
    if errors:
        print("\n=== Errors / skipped ===")
        for e in errors:
            print(f"  {e}")
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--images-dir", required=True, type=Path, action="append",
                        help="repeatable; pairs with the --ground-truth at the same position")
    parser.add_argument("--ground-truth", required=True, type=Path, action="append")
    parser.add_argument("--sweep", action="store_true",
                        help="Run the approval-tier sweep and the confidence_floor sweep instead of the accuracy report")
    parser.add_argument("--sweep-start", type=float, default=0.0)
    parser.add_argument("--sweep-stop", type=float, default=1.0)
    parser.add_argument("--sweep-step", type=float, default=0.05)
    args = parser.parse_args()
    if len(args.images_dir) != len(args.ground_truth):
        parser.error("give one --ground-truth for each --images-dir")

    results: list[FileResult] = []
    for images_dir, ground_truth_path in zip(args.images_dir, args.ground_truth):
        ground_truth = json.loads(ground_truth_path.read_text(encoding="utf-8"))
        results.extend(evaluate_all(images_dir, ground_truth))

    if args.sweep:
        thresholds = []
        t = args.sweep_start
        while t <= args.sweep_stop + 1e-9:
            thresholds.append(round(t, 2))
            t += args.sweep_step
        code = approval_sweep(results, _approval_grid(max(args.sweep_start, 0.5), args.sweep_stop, args.sweep_step))
        print("=" * 78)
        return max(code, confidence_sweep(results, thresholds))

    return accuracy_report(results)


if __name__ == "__main__":
    sys.exit(main())
