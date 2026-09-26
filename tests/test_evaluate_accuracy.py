import io

import fitz
from PIL import Image

from app.modules.type_identification import IdentificationResult
from scripts.evaluate_accuracy import (
    OUTCOME_CORRECT,
    OUTCOME_AMBIGUOUS,
    OUTCOME_NEW_TEMPLATE,
    OUTCOME_WRONG,
    TIER_CAMERA_JPEG,
    TIER_OTHER_IMAGE,
    TIER_SCANNED_PDF,
    TIER_TEXT_PDF,
    FieldOutcome,
    FileResult,
    _approval_grid,
    _classify,
    _tier_at,
    approval_sweep,
    accuracy_report,
    detect_tier,
)


def _image_bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (120, 90), color=(200, 200, 200)).save(buf, format=fmt)
    return buf.getvalue()


def _pdf_bytes(*, text_on_pages) -> bytes:
    """One page per entry: a string draws that text, None draws only a raster
    image (which is all a scanned PDF is)."""
    doc = fitz.open()
    for text in text_on_pages:
        page = doc.new_page()
        if text is None:
            page.insert_image(fitz.Rect(50, 50, 300, 300), stream=_image_bytes("PNG"))
        else:
            page.insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def test_pdf_with_embedded_text_is_the_text_layer_tier():
    assert detect_tier(_pdf_bytes(text_on_pages=["AIR WAYBILL"])) == TIER_TEXT_PDF


def test_pdf_that_is_only_a_raster_image_is_the_scanned_tier():
    assert detect_tier(_pdf_bytes(text_on_pages=[None])) == TIER_SCANNED_PDF


def test_any_page_with_text_makes_the_pdf_text_layer():
    # Page 1 is a bare scan, page 2 has text: the file as a whole has a text layer.
    assert detect_tier(_pdf_bytes(text_on_pages=[None, "page two"])) == TIER_TEXT_PDF


def test_jpeg_is_camera_photo_and_png_is_reported_separately():
    assert detect_tier(_image_bytes("JPEG")) == TIER_CAMERA_JPEG
    assert detect_tier(_image_bytes("PNG")) == TIER_OTHER_IMAGE


def test_classification_outcomes_are_distinguished():
    def result(**kw):
        defaults = dict(confirmed=False, template_id=None, version=None, reason=None)
        return IdentificationResult(**{**defaults, **kw})

    assert _classify(result(confirmed=True, template_id="a", version=1), "a") == OUTCOME_CORRECT
    assert _classify(result(confirmed=True, template_id="b", version=1), "a") == OUTCOME_WRONG
    assert _classify(result(reason="ambiguous_match"), "a") == OUTCOME_AMBIGUOUS
    assert _classify(result(reason="no_match"), "a") == OUTCOME_NEW_TEMPLATE


def _file(name, tier, classification, correct, total, skipped=None):
    fields = [FieldOutcome(f"f{i}", "x", "x" if i < correct else "y", i < correct, 90.0) for i in range(total)]
    return FileResult(
        filename=name,
        tier=tier,
        expected_template="t",
        classification=classification,
        fields=[] if skipped else fields,
        extraction_skipped=skipped,
    )


def test_report_separates_tiers_shows_empty_ones_and_keeps_the_overall(capsys):
    accuracy_report(
        [
            _file("a.pdf", TIER_TEXT_PDF, OUTCOME_CORRECT, 9, 10),
            _file("b.pdf", TIER_TEXT_PDF, OUTCOME_CORRECT, 10, 10),
            _file("c.jpg", TIER_CAMERA_JPEG, OUTCOME_NEW_TEMPLATE, 2, 4),
        ]
    )
    out = capsys.readouterr().out

    assert "19/20" in out and "95.0%" in out  # text-layer tier: 9+10 of 20
    assert "2/4" in out and "50.0%" in out  # camera tier
    assert "21/24" in out and "87.5%" in out  # the blended overall, still reported
    # The tier with no files is shown as empty rather than silently omitted.
    scanned_row = next(line for line in out.splitlines() if "scanned / flat-image PDF" in line)
    assert "no samples" in scanned_row
    # Classification is reported in its own table, independent of field accuracy.
    assert "Module 5 classification" in out


def test_files_whose_extraction_could_not_run_are_counted_but_add_no_fields(capsys):
    accuracy_report(
        [
            _file("ok.pdf", TIER_TEXT_PDF, OUTCOME_CORRECT, 5, 5),
            _file("bad.pdf", TIER_TEXT_PDF, OUTCOME_NEW_TEMPLATE, 0, 0, skipped="anchor verification failed"),
        ]
    )
    out = capsys.readouterr().out

    text_row = next(line for line in out.splitlines() if line.strip().startswith("text-layer PDF"))
    assert "5/5" in text_row and "100.0%" in text_row  # the skipped file must not drag it down or pad it
    assert "anchor verification failed" in out  # ...but it is reported, never silent


def test_report_says_which_path_read_each_file(capsys):
    native = _file("a.pdf", TIER_TEXT_PDF, OUTCOME_CORRECT, 3, 3)
    native.text_native = True
    ocr = _file("b.jpg", TIER_CAMERA_JPEG, OUTCOME_CORRECT, 3, 3)

    accuracy_report([native, ocr])
    out = capsys.readouterr().out

    assert "a.pdf" in next(l for l in out.splitlines() if "read via: text layer, no OCR" in l)
    assert "b.jpg" in next(l for l in out.splitlines() if l.rstrip().endswith("read via: OCR"))


# --- approval-tier sweep ----------------------------------------------------------------------

def _outcome(field_id, correct, confidence, accepted=True, has_validator=True):
    return FieldOutcome(field_id, "x", "x" if correct else "y", correct, confidence, accepted, has_validator)


def _camera_file(*outcomes, name="synthetic__a.jpg"):
    return FileResult(filename=name, tier=TIER_CAMERA_JPEG, expected_template="t", classification=OUTCOME_CORRECT,
                      fields=list(outcomes))


def _cells(out, threshold):
    """[auto, precision, wrong-in-auto, spot_check, review] for one row of the sweep table."""
    line = next(line for line in out.splitlines() if line.strip().startswith(f"{threshold:.2f} |"))
    return [cell.split("<-")[0].strip() for cell in line.split("|")[1:]]


def test_the_sweep_reports_precision_of_the_auto_subset_and_the_tier_split(capsys):
    approval_sweep(
        [_camera_file(
            _outcome("a", True, 95.0), _outcome("b", True, 92.0),
            _outcome("c", False, 85.0),                      # wrong AND accepted: the dangerous kind
            _outcome("d", False, 40.0, accepted=False),      # wrong but rejected: caught, so never auto
        )],
        thresholds=[0.80, 0.90],
    )
    out = capsys.readouterr().out
    camera = out[out.index("camera-photo JPEG"):]

    # at 0.80 a, b and c are auto and c is wrong; at 0.90 c drops to spot_check; d is review at any threshold
    assert _cells(camera, 0.80) == ["3 (75%)", "2/3 =  66.7%", "1", "0%", "25%"]
    assert _cells(camera, 0.90) == ["2 (50%)", "2/2 = 100.0%", "0", "25%", "25%"]


def test_the_sweep_lists_the_wrong_values_module_7_accepted_and_only_those(capsys):
    approval_sweep(
        [_camera_file(_outcome("shipper_name", False, 85.6, has_validator=False),
                      _outcome("rejected_one", False, 40.0, accepted=False), _outcome("fine", True, 95.0))],
        thresholds=[0.90],
    )
    out = capsys.readouterr().out

    assert "wrong values Module 7 accepted (1)" in out
    assert "shipper_name" in out and "rejected_one" not in out
    assert "confidence only" in out  # says nothing but confidence could have caught it


def test_the_sweep_never_auto_approves_a_field_with_no_validator(capsys):
    """However high the confidence and however low the threshold: the rule Module 7 applies."""
    approval_sweep(
        [_camera_file(_outcome("checked", True, 99.0), _outcome("unchecked", True, 99.0, has_validator=False),
                      _outcome("unchecked_wrong", False, 99.0, has_validator=False))],
        thresholds=[0.50, 0.90],
    )
    out = capsys.readouterr().out
    camera = out[out.index("camera-photo JPEG"):]

    assert _cells(camera, 0.50) == ["1 (33%)", "1/1 = 100.0%", "0", "67%", "0%"]
    assert "2 read(s) confident enough for auto held at spot_check for having no regex/checksum (1 correct, 1 wrong)" in out


def test_a_threshold_only_clears_the_target_if_every_higher_one_does(capsys):
    """Precision that dips again higher up is luck, not a threshold to ship."""
    outs = [_outcome(f"ok{i}", True, 96.0) for i in range(99)] + [_outcome("bad", False, 96.0)]  # 99/100 at any threshold <= 0.96
    approval_sweep([_camera_file(*outs, _outcome("hi", False, 99.0))], thresholds=[0.50, 0.97, 0.98])
    out = capsys.readouterr().out

    # at 0.97 and 0.98 only the one wrong 99.0 read is auto (0/1): never clears
    assert "no threshold on the grid gives auto precision >= 99%" in out


def test_the_sweep_says_when_a_tier_has_no_samples(capsys):
    approval_sweep([_camera_file(_outcome("a", True, 95.0))], thresholds=[0.90])
    scanned = next(chunk for chunk in capsys.readouterr().out.split("=== ") if "scanned / flat-image PDF" in chunk)
    assert "no samples" in scanned


def test_the_sweep_warns_that_reads_are_not_independent_samples(capsys):
    approval_sweep([_camera_file(_outcome("a", True, 95.0), name="a.jpg"), _camera_file(_outcome("a", True, 95.0), name="b.jpg")],
                   thresholds=[0.90])
    out = capsys.readouterr().out
    assert "2 field reads from 2 file(s)" in out and "1 DISTINCT expected values" in out


def test_real_and_synthetic_reads_are_told_apart(capsys):
    approval_sweep([_camera_file(_outcome("a", True, 95.0), name="synthetic__x.jpg"),
                    _camera_file(_outcome("a", True, 95.0), name="bits_goa_id_card/sample_01.jpg")], thresholds=[0.90])
    assert "(1 from real documents)" in capsys.readouterr().out


def test_the_harness_tier_rule_is_module_7s_rule(monkeypatch):
    """The sweep re-implements the rule so it can vary the threshold; it must not drift from the real one."""
    from app.config import CONFIDENCE_AUTO_APPROVE_THRESHOLD
    from app.modules.validation import approval_tier

    for accepted in (True, False):
        for validated in (True, False):
            for confidence in (0.0, 50.0, CONFIDENCE_AUTO_APPROVE_THRESHOLD * 100 - 0.1, CONFIDENCE_AUTO_APPROVE_THRESHOLD * 100, 100.0):
                outcome = _outcome("f", True, confidence, accepted, has_validator=validated)
                assert _tier_at(outcome, CONFIDENCE_AUTO_APPROVE_THRESHOLD) == approval_tier(accepted, confidence, validated)


def test_the_grid_is_fine_where_the_decision_is_made():
    grid = _approval_grid(0.5, 1.0, 0.05)
    assert 0.90 in grid and 0.93 in grid and 1.0 in grid and grid == sorted(grid)
    assert 0.53 not in grid


def test_two_corpora_need_two_ground_truths(monkeypatch):
    import sys

    import pytest

    from scripts.evaluate_accuracy import main

    monkeypatch.setattr(sys, "argv", ["evaluate_accuracy.py", "--images-dir", "a", "--images-dir", "b", "--ground-truth", "gt.json"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
