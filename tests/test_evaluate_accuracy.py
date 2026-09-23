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
    _classify,
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
