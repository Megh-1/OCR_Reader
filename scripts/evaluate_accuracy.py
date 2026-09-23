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
import pytesseract  # noqa: E402

from app.config import TESSERACT_CMD  # noqa: E402
from app.modules.ingestion import extract_text_layers, ingest_file  # noqa: E402
from app.modules.pipeline import extract_page_fields  # noqa: E402
from app.modules.preprocessing import preprocess_image  # noqa: E402
from app.modules.template_store import get_latest_template, get_template_version, list_known_doc_types  # noqa: E402
from app.modules.type_identification import identify_document_type  # noqa: E402

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

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
    parser.add_argument("--images-dir", required=True, type=Path)
    parser.add_argument("--ground-truth", required=True, type=Path)
    parser.add_argument("--sweep", action="store_true", help="Run the confidence_floor sweep instead of the accuracy report")
    parser.add_argument("--sweep-start", type=float, default=0.0)
    parser.add_argument("--sweep-stop", type=float, default=1.0)
    parser.add_argument("--sweep-step", type=float, default=0.05)
    args = parser.parse_args()

    ground_truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))
    results = evaluate_all(args.images_dir, ground_truth)

    if args.sweep:
        thresholds = []
        t = args.sweep_start
        while t <= args.sweep_stop + 1e-9:
            thresholds.append(round(t, 2))
            t += args.sweep_step
        return confidence_sweep(results, thresholds)

    return accuracy_report(results)


if __name__ == "__main__":
    sys.exit(main())
