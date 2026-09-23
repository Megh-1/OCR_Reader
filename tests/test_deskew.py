"""
Module 2 deskew: measuring skew, correcting it WITHOUT moving the frame, capping
the correction, and skipping it for text-native pages.
"""

import io
import logging
import re
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from PIL import Image

import app.modules.field_extraction as field_extraction
import app.modules.pipeline as pipeline
import app.modules.preprocessing as preprocessing
import app.modules.type_identification as type_identification
from app.modules.preprocessing import (
    decode_image,
    estimate_skew_angle,
    estimate_skew_via_hough,
    preprocess_image,
)
from tests.test_text_native import _post_pdf, box_for, field, save_template, scan_pdf, text_pdf

W, H = 1200, 1600
# A solid marker in the top-left, where a canvas that grows or shifts moves it most.
MARKER = (0.15, 0.12, 0.27, 0.19)  # x0, y0, x1, y1 as fractions of the page


def ruled_page(ccw_degrees: float = 0.0) -> np.ndarray:
    """A white page with full-width rules standing in for lines of text, plus a
    solid marker at a known fractional position, turned IN ITS FRAME by
    `ccw_degrees` (cv2's convention: positive is counter-clockwise) - which is
    what a skewed scan is."""
    img = np.full((H, W), 255, np.uint8)
    for y in range(240, H - 200, 70):
        cv2.line(img, (140, y), (W - 140, y), 0, 3)
    x0, y0, x1, y1 = MARKER
    cv2.rectangle(img, (int(x0 * W), int(y0 * H)), (int(x1 * W), int(y1 * H)), 0, -1)
    if ccw_degrees:
        matrix = cv2.getRotationMatrix2D((W / 2, H / 2), ccw_degrees, 1.0)
        img = cv2.warpAffine(img, matrix, (W, H), borderValue=255)
    return img


def png(gray: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(gray).save(buf, format="PNG")
    return buf.getvalue()


def marker_fractions(processed_field_image: bytes) -> tuple[float, float, float, float]:
    """Where the solid marker sits in a processed image, as page fractions.
    Thin rules are opened away so only the solid block remains."""
    gray = np.array(Image.open(io.BytesIO(processed_field_image)).convert("L"))
    dark = (gray < 100).astype(np.uint8)
    solid = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((25, 25), np.uint8))
    ys, xs = np.where(solid > 0)
    h, w = gray.shape
    return xs.min() / w, ys.min() / h, xs.max() / w, ys.max() / h


# --- the rotation itself -------------------------------------------------------


def test_rotation_keeps_the_frame_by_default_and_can_still_be_asked_to_grow():
    page = ruled_page()
    assert preprocessing.correct_rotation(page, 3.0).shape == page.shape

    grown = preprocessing.correct_rotation(page, 3.0, expand=True)
    assert grown.shape[0] > page.shape[0] and grown.shape[1] > page.shape[1]


def test_a_deskewed_page_keeps_its_content_at_the_same_fractional_position():
    """The property the previous, canvas-growing rotation broke. Every template
    bbox is a percentage of the frame, so deskew must hand back the page in the
    frame it came in - not padded with white and scaled down inside it."""
    processed = preprocess_image(png(ruled_page(ccw_degrees=2.5)))

    assert processed.deskew_applied
    got = marker_fractions(processed.field_image_bytes)
    assert got == pytest.approx(MARKER, abs=0.012)


def test_the_canvas_growing_rotation_would_have_moved_the_marker(monkeypatch):
    """Locks in WHY the default changed: the same page, deskewed with the old
    rotation, lands the marker measurably off its cell."""
    real = preprocessing.correct_rotation
    monkeypatch.setattr(preprocessing, "correct_rotation", lambda image, angle: real(image, angle, expand=True))

    got = marker_fractions(preprocess_image(png(ruled_page(ccw_degrees=2.5))).field_image_bytes)

    assert max(abs(g - m) for g, m in zip(got, MARKER)) > 0.012  # outside the tolerance the correct rotation meets


# --- measuring skew ------------------------------------------------------------


@pytest.mark.parametrize("ccw", [1.5, -2.5, 6.0])
def test_the_estimator_recovers_the_injected_angle(ccw):
    gray = ruled_page(ccw_degrees=ccw)

    estimate = estimate_skew_angle(gray)

    # cv2 rotates counter-clockwise; Module 2 reports clockwise-positive.
    assert estimate.angle == pytest.approx(-ccw, abs=0.3)
    assert estimate.method == "contour"


def test_hough_recovers_the_injected_angle_and_declines_a_page_with_no_lines():
    assert estimate_skew_via_hough(ruled_page(ccw_degrees=3.0)) == pytest.approx(-3.0, abs=0.3)
    assert estimate_skew_via_hough(np.full((H, W), 255, np.uint8)) is None


def test_hough_is_the_fallback_when_the_contour_mass_is_untrustworthy(monkeypatch):
    monkeypatch.setattr(preprocessing, "MIN_CONTOUR_AREA_FRACTION", 1.0)  # no contour can qualify

    estimate = estimate_skew_angle(ruled_page(ccw_degrees=3.0))

    assert estimate.method == "hough"
    assert estimate.used_osd_fallback is False
    assert estimate.angle == pytest.approx(-3.0, abs=0.3)


def test_osd_is_only_the_last_resort(monkeypatch):
    monkeypatch.setattr(preprocessing.pytesseract, "image_to_osd", lambda _img: "Rotate: 0\n")

    estimate = estimate_skew_angle(np.full((H, W), 255, np.uint8))  # nothing for contour or Hough to use

    assert estimate.method == "osd"
    assert estimate.used_osd_fallback is True


# --- the cap -------------------------------------------------------------------


def test_a_correction_beyond_the_cap_is_left_alone_and_logged(caplog):
    caplog.set_level(logging.WARNING, logger="app")

    processed = preprocess_image(png(ruled_page(ccw_degrees=14.0)))

    assert processed.deskew_applied is False
    assert processed.skew_angle_corrected == 0.0
    assert processed.skew_angle_detected == pytest.approx(-14.0, abs=1.0)  # measured, just not applied
    assert any("exceeds DESKEW_MAX_ANGLE_CORRECTION" in r.getMessage() for r in caplog.records)


def test_a_correction_within_the_cap_is_applied():
    processed = preprocess_image(png(ruled_page(ccw_degrees=8.0)))
    assert processed.deskew_applied is True
    assert processed.skew_angle_corrected == pytest.approx(-8.0, abs=0.5)


def test_deskew_can_be_switched_off():
    processed = preprocess_image(png(ruled_page(ccw_degrees=2.5)), deskew=False)

    assert processed.deskew_method == "disabled"
    assert processed.deskew_applied is False
    assert processed.skew_angle_detected == 0.0


# --- the pipeline: skipped for text-native pages, logged for the rest ----------


def _spy_on_preprocessing(monkeypatch):
    calls = []
    real = pipeline.preprocess_image

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(pipeline, "preprocess_image", spy)
    return calls


def test_a_text_native_page_skips_deskew_entirely(client, unique_doc_type, monkeypatch, caplog):
    pdf = text_pdf([("ACME EXPORTS LTD", 72, 120)])
    save_template(client, unique_doc_type, [field("vendor", box_for(pdf, "ACME EXPORTS LTD"))])
    calls = _spy_on_preprocessing(monkeypatch)
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", Mock(side_effect=AssertionError("OCR ran")))
    caplog.set_level(logging.INFO, logger="app")

    resp = _post_pdf(client, pdf, unique_doc_type)

    assert resp.json()["pages"][0]["text_native"] is True
    assert calls[0]["deskew"] is False
    assert any("deskew: skipped (text-native)" in r.getMessage() for r in caplog.records)


def test_a_scanned_page_is_deskewed_and_its_angle_is_logged(client, unique_doc_type, monkeypatch, caplog):
    save_template(client, unique_doc_type, [field("vendor", [0.1, 0.1, 0.5, 0.2])])
    calls = _spy_on_preprocessing(monkeypatch)
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(
        field_extraction.pytesseract, "image_to_data", Mock(return_value={"text": ["X"], "conf": [90]})
    )
    caplog.set_level(logging.INFO, logger="app")

    resp = _post_pdf(client, scan_pdf(), unique_doc_type)

    assert resp.json()["pages"][0]["text_native"] is False
    assert calls[0]["deskew"] is True
    line = next(r.getMessage() for r in caplog.records if "deskew: detected=" in r.getMessage())
    assert re.fullmatch(r"page 0 deskew: detected=[+-]\d+\.\d{2} deg method=\w+ applied=(True|False)", line), line
