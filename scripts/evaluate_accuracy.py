#!/usr/bin/env python
"""
Accuracy evaluation harness (tooling only - Phase D).

Per the reference doc's testing strategy: measure per-field accuracy
against hand-verified ground truth, then tune each field's confidence_floor
until the false-accept rate is ~0. This script does the measuring; tuning
based on its output is a separate, human judgment call, not automated here.

Usage:
    python scripts/evaluate_accuracy.py --images-dir DIR --ground-truth FILE.json
    python scripts/evaluate_accuracy.py --images-dir DIR --ground-truth FILE.json --sweep

Ground truth JSON shape:
    {
      "sample_01.jpg": {
        "template_id": "bits_goa_id_card",
        "version": 1,                 // optional - defaults to latest
        "fields": {"student_name": "John Doe", "id_number": "2024A7PS0476G"}
      },
      ...
    }

type_hint is always passed as the ground truth's template_id - this harness
measures FIELD accuracy against a known-correct template, not Module 5's
identification accuracy (that's a different, separate question).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytesseract  # noqa: E402

from app.config import TESSERACT_CMD  # noqa: E402
from app.modules.field_extraction import extract_fields  # noqa: E402
from app.modules.preprocessing import preprocess_image  # noqa: E402
from app.modules.type_identification import identify_document_type  # noqa: E402

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def run_extraction(image_path: Path, template_id: str, version: Optional[int]):
    """Runs the real Modules 2, 5, and 6 against one sample image."""
    processed = preprocess_image(image_path.read_bytes())

    identification = identify_document_type(processed.image_bytes, type_hint=template_id)
    if not identification.confirmed:
        raise RuntimeError(
            f"anchor verification failed ({identification.reason}) - fix the template before measuring field accuracy"
        )

    resolved_version = version or identification.version
    return extract_fields(template_id, resolved_version, processed.image_bytes)


def _iter_samples(images_dir: Path, ground_truth: dict):
    for filename, expected in ground_truth.items():
        image_path = images_dir / filename
        if not image_path.exists():
            yield filename, expected, None, f"{filename}: image not found at {image_path}"
            continue
        try:
            extractions = run_extraction(image_path, expected["template_id"], expected.get("version"))
        except Exception as exc:
            yield filename, expected, None, f"{filename}: {exc}"
            continue
        yield filename, expected, extractions, None


def accuracy_report(images_dir: Path, ground_truth: dict) -> int:
    per_field_correct: dict[str, int] = {}
    per_field_total: dict[str, int] = {}
    mismatches: list[str] = []
    errors: list[str] = []

    for filename, expected, extractions, error in _iter_samples(images_dir, ground_truth):
        if error:
            errors.append(error)
            continue
        for field_id, expected_value in expected["fields"].items():
            per_field_total[field_id] = per_field_total.get(field_id, 0) + 1
            actual = extractions.get(field_id)
            actual_value = actual.raw_text if actual else ""
            if normalize(actual_value) == normalize(expected_value):
                per_field_correct[field_id] = per_field_correct.get(field_id, 0) + 1
            else:
                conf = actual.aggregate_confidence if actual else "n/a"
                mismatches.append(f"{filename} / {field_id}: expected {expected_value!r}, got {actual_value!r} (confidence={conf})")

    print("=== Per-field accuracy ===")
    for field_id in sorted(per_field_total):
        correct = per_field_correct.get(field_id, 0)
        total = per_field_total[field_id]
        print(f"  {field_id}: {correct}/{total} ({100 * correct / total:.1f}%)")

    if mismatches:
        print("\n=== Mismatches ===")
        for m in mismatches:
            print(f"  {m}")

    if errors:
        print("\n=== Errors (samples skipped) ===")
        for e in errors:
            print(f"  {e}")

    return 1 if errors else 0


def confidence_sweep(images_dir: Path, ground_truth: dict, thresholds: list[float]) -> int:
    per_field_samples: dict[str, list[tuple[bool, float]]] = {}
    errors: list[str] = []

    for filename, expected, extractions, error in _iter_samples(images_dir, ground_truth):
        if error:
            errors.append(error)
            continue
        for field_id, expected_value in expected["fields"].items():
            actual = extractions.get(field_id)
            if actual is None:
                continue
            correct = normalize(actual.raw_text) == normalize(expected_value)
            per_field_samples.setdefault(field_id, []).append((correct, actual.aggregate_confidence))

    field_ids = sorted(per_field_samples)
    print("=== Confidence floor sweep (false-accept rate per threshold) ===")
    print("  threshold | " + " | ".join(field_ids))
    for threshold in thresholds:
        row = [f"{threshold:.2f}"]
        for field_id in field_ids:
            samples = per_field_samples[field_id]
            accepted = [correct for correct, conf in samples if conf >= threshold * 100]
            false_accepts = sum(1 for correct in accepted if not correct)
            row.append(f"{false_accepts / len(accepted):.2f}" if accepted else "n/a")
        print("  " + " | ".join(row))

    if errors:
        print("\n=== Errors (samples skipped) ===")
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

    if args.sweep:
        thresholds = []
        t = args.sweep_start
        while t <= args.sweep_stop + 1e-9:
            thresholds.append(round(t, 2))
            t += args.sweep_step
        return confidence_sweep(args.images_dir, ground_truth, thresholds)

    return accuracy_report(args.images_dir, ground_truth)


if __name__ == "__main__":
    sys.exit(main())
