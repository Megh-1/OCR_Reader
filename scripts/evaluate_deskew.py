#!/usr/bin/env python
"""
Measures Module 2's deskew against the skew and rotation the synthetic corpus
generator KNEW it injected (recorded per file in the corpus ground_truth.json).

For each file it reports:

  detected vs injected   what the skew estimator (contour, then Hough, then OSD)
                         measures on the raw image, against the recorded angle.
                         Clockwise-positive, Module 2's convention. For a photo the
                         injected angle is the warped page's TOP edge - the direction
                         the text lines run - since that is what a deskew is trying
                         to recover; the left edge is shown too, and the gap between
                         the two is perspective.
  the method the pipeline actually used
                         "document_detected" means the page was found and flattened by
                         the four-point warp, which corrects rotation AND perspective
                         at once, so the skew estimator never ran on that file.
  field accuracy, four ways, all with the correct type_hint so the number isolates
  extraction from classification:
     no deskew           preprocess_image(deskew=False)
     pipeline            the real path
     expanding (old)     the same deskew but with the canvas-enlarging rotation this
                         module used to apply, kept here so the bug that made deskew
                         hurt stays visible
     deskew ONLY         document detection switched off, so the only geometric
                         correction is rotation - the honest test of whether simple
                         deskew can handle a file. Keystone is perspective, not
                         rotation, so it is expected to fail here.

Everything is measured on SYNTHETIC data. See config.DESKEW_MAX_ANGLE_CORRECTION.

    python scripts/evaluate_deskew.py
    python scripts/evaluate_deskew.py --corpus-dir tests/fixtures/synthetic_awb --files synthetic__scan_skew1p5_noise.pdf
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import cv2  # noqa: E402

import app.modules.preprocessing as preprocessing  # noqa: E402
from app.runtime import configure_tesseract  # noqa: E402
from app.modules.field_extraction import extract_fields  # noqa: E402
from app.modules.ingestion import ingest_file  # noqa: E402
from app.modules.pipeline import preprocess_for_template  # noqa: E402
from app.modules.preprocessing import decode_image, estimate_skew_angle, estimate_skew_via_hough, preprocess_image  # noqa: E402
from app.modules.template_store import get_latest_template  # noqa: E402
from app.modules.type_identification import identify_document_type  # noqa: E402

configure_tesseract()

DEFAULT_FILES = [
    "synthetic__scan_clean_200dpi.pdf",  # control: nothing injected
    "synthetic__scan_skew1p5_noise.pdf",
    "synthetic__scan_skew_neg2p5_jpegq55.pdf",
    "synthetic__photo_rotated.jpg",
    "synthetic__photo_keystone.jpg",
    "synthetic__photo_corner_perspective.jpg",
    "synthetic__photo_harsh_combined.jpg",
]
FAIL_BELOW = 0.5  # a file "fails" a mode when it is refused or gets under this share of fields


def normalize(value: str) -> str:
    return " ".join(value.strip().lower().split())


def injected_angle(entry: dict):
    """(headline angle, note) for the entry, or (None, ...) when nothing was recorded."""
    injected = entry.get("injected")
    if not injected:
        return None, "no injected metadata (rebuild or run build_synthetic_corpus.py --annotate-only)"
    if injected["kind"] == "skew":
        return injected["skew_cw_degrees"], "skew"
    top, left = injected["top_edge_cw_degrees"], injected["left_edge_cw_degrees"]
    return top, f"{injected['kind']}  (left edge {left:+.2f})"


def score(page: bytes, template, expected: dict, *, deskew: bool, detect_document: bool, expanding: bool = False):
    """(fields correct, fields total) with the correct type_hint, or (None, reason)
    when the anchors are refused and the pipeline would extract nothing."""
    def run():
        generic = preprocess_image(page, deskew=deskew)
        identification = identify_document_type(generic.image_bytes, type_hint=template.template_id)
        if not identification.confirmed:
            return None, f"anchors refused ({identification.reason})"
        processed = preprocess_for_template(page, template, generic, deskew=deskew)
        extractions = extract_fields(template.template_id, template.version, processed.field_image_bytes)
        correct = sum(
            normalize(extractions[f].raw_text if f in extractions else "") == normalize(v) for f, v in expected.items()
        )
        return correct, len(expected)

    with ExitStack() as stack:
        if not detect_document:
            stack.enter_context(patch.object(preprocessing, "detect_document_quad", lambda gray: None))
        if expanding:
            real = preprocessing.correct_rotation
            stack.enter_context(
                patch.object(preprocessing, "correct_rotation", lambda image, angle: real(image, angle, expand=True))
            )
        return run()


def cell(result) -> str:
    correct, total = result
    return f"{correct}/{total}" if correct is not None else "REFUSED"


def failed(result) -> bool:
    correct, total = result
    return correct is None or correct < FAIL_BELOW * total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus-dir", type=Path, default=PROJECT_ROOT / "tests" / "fixtures" / "synthetic_awb")
    parser.add_argument("--files", nargs="*", default=DEFAULT_FILES)
    args = parser.parse_args()

    ground_truth = json.loads((args.corpus_dir / "ground_truth.json").read_text(encoding="utf-8"))
    template = get_latest_template("air_waybill")
    if template is None:
        raise SystemExit("no air_waybill template - run scripts/seed_templates.py")

    rows = []
    for name in args.files:
        entry = ground_truth[name]
        data = (args.corpus_dir / name).read_bytes()
        page = ingest_file(data, name, None)[0][0]

        gray = cv2.cvtColor(decode_image(page), cv2.COLOR_BGR2GRAY)
        estimate = estimate_skew_angle(gray)
        hough = estimate_skew_via_hough(gray)
        used = preprocess_image(page)  # what the real pipeline decided for this file
        injected, note = injected_angle(entry)

        rows.append(
            {
                "name": name,
                "note": note,
                "injected": injected,
                "estimate": estimate,
                "hough": hough,
                "pipeline_method": used.deskew_method,
                "pipeline_applied": used.skew_angle_corrected,
                "pipeline_did_rotate": used.deskew_applied,
                "no_deskew": score(page, template, entry["fields"], deskew=False, detect_document=True),
                "pipeline": score(page, template, entry["fields"], deskew=True, detect_document=True),
                "expanding": score(page, template, entry["fields"], deskew=True, detect_document=True, expanding=True),
                "deskew_only": score(page, template, entry["fields"], deskew=True, detect_document=False),
            }
        )
        print(f"  measured {name}", file=sys.stderr)

    print("Angles: degrees, clockwise-positive. Injected = what the generator applied.\n")
    print(f"{'file':42} {'injected':>9} {'estimator':>10} {'err':>6} {'method':>8} {'hough-only':>11}   pipeline used")
    for r in rows:
        inj = r["injected"]
        err = f"{r['estimate'].angle - inj:+.2f}" if inj is not None else "n/a"
        hough = f"{r['hough']:+.2f}" if r["hough"] is not None else "none"
        if r["pipeline_method"] in ("document_detected", "disabled"):
            used = r["pipeline_method"]
        elif r["pipeline_did_rotate"]:
            used = f"{r['pipeline_method']} ({r['pipeline_applied']:+.2f} applied)"
        else:
            used = f"{r['pipeline_method']} ({r['pipeline_applied']:+.2f}, below no-op threshold - not rotated)"
        print(
            f"{r['name']:42} {(f'{inj:+.2f}' if inj is not None else 'n/a'):>9} {r['estimate'].angle:>+10.2f} "
            f"{err:>6} {r['estimate'].method:>8} {hough:>11}   {used}"
        )
        print(f"{'':42} {r['note']}")

    print(f"\nField accuracy, correct type_hint (a file FAILS a mode when refused or under {FAIL_BELOW:.0%} of fields):")
    print(f"{'file':42} {'no deskew':>10} {'expanding':>10} {'pipeline':>10} {'deskew only':>12}")
    for r in rows:
        print(
            f"{r['name']:42} {cell(r['no_deskew']):>10} {cell(r['expanding']):>10} "
            f"{cell(r['pipeline']):>10} {cell(r['deskew_only']):>12}"
        )

    print("\nReading the table:")
    for r in rows:
        name = r["name"]
        if r["pipeline_method"] == "document_detected":
            same = "identical" if r["no_deskew"] == r["pipeline"] else "DIFFERENT"
            print(
                f"  {name}: page was found and flattened by document detection, so the skew estimator never ran "
                f"in the pipeline - no-deskew and pipeline are {same}."
            )
        elif r["no_deskew"] != r["pipeline"]:
            print(f"  {name}: deskew moved accuracy {cell(r['no_deskew'])} -> {cell(r['pipeline'])}.")
        elif abs(r["pipeline_applied"]) >= preprocessing.SKEW_ANGLE_NOOP_THRESHOLD:
            print(
                f"  {name}: deskew measured and applied {r['pipeline_applied']:+.2f} deg but accuracy did not move "
                f"({cell(r['pipeline'])}) - this file is limited by something other than skew."
            )
        if r["expanding"] != r["pipeline"] and r["pipeline_method"] != "document_detected":
            print(
                f"  {name}: the old canvas-expanding rotation scored {cell(r['expanding'])} against "
                f"{cell(r['pipeline'])} now."
            )
        if r["pipeline_method"] == "document_detected" and failed(r["deskew_only"]):
            kind = "keystone (perspective, not rotation)" if "perspective" in r["note"] and "rotation" not in r["note"].split("(")[0] else "rotation + framing"
            print(
                f"  {name}: FAILS with deskew alone ({cell(r['deskew_only'])}) - expected for {kind}; rotation cannot fix it. "
                f"It passes in the pipeline ({cell(r['pipeline'])}) only because the four-point warp corrects perspective."
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
