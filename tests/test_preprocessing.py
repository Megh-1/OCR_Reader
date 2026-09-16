import io

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

import app.modules.preprocessing as preprocessing


def make_text_like_image(size: tuple[int, int] = (600, 400)) -> Image.Image:
    """A synthetic 'page': evenly spaced horizontal bars standing in for text lines."""
    img = Image.new("L", size, color=255)
    draw = ImageDraw.Draw(img)
    for y in range(40, size[1] - 40, 30):
        draw.rectangle([40, y, size[0] - 40, y + 12], fill=0)
    return img


def make_faded_image(size: tuple[int, int] = (300, 200)) -> np.ndarray:
    """Low-contrast text-like image: faint gray bars on a near-matching gray background."""
    img = Image.new("L", size, color=200)
    draw = ImageDraw.Draw(img)
    for y in range(20, size[1] - 20, 20):
        draw.rectangle([20, y, size[0] - 20, y + 8], fill=170)
    arr = np.array(img).astype(np.float32)
    noise = np.random.default_rng(1).normal(0, 2, arr.shape)
    return np.clip(arr + noise, 0, 255).astype(np.uint8)


def encode_png(pil_image: Image.Image) -> bytes:
    buf = io.BytesIO()
    pil_image.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def test_upright_scan_has_near_zero_correction():
    result = preprocessing.preprocess_image(encode_png(make_text_like_image()), source_dpi=300)
    assert abs(result.skew_angle_corrected) < 1.0
    assert result.osd_fallback_used is False


def test_rotated_image_is_corrected_to_upright():
    rotated = make_text_like_image().rotate(7, expand=True, fillcolor=255)
    gray = np.array(rotated)

    skew = preprocessing.estimate_skew_angle(gray)
    assert skew.used_osd_fallback is False
    assert abs(skew.angle) > 2.0  # a real, non-trivial skew was actually detected

    corrected_bgr = preprocessing.correct_rotation(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), skew.angle)
    corrected_gray = cv2.cvtColor(corrected_bgr, cv2.COLOR_BGR2GRAY)
    residual = preprocessing.estimate_skew_angle(corrected_gray)

    # If the correction direction were backwards, this would double the skew
    # (~14 deg) instead of cancelling it - this is the direction check.
    assert abs(residual.angle) < 1.5


def test_denoise_and_enhance_improves_contrast():
    faded = make_faded_image()
    enhanced = preprocessing.denoise_and_enhance(faded)
    assert enhanced.std() > faded.std()


def test_resolution_normalization_matches_target_dpi(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(preprocessing, "TARGET_DPI", 300.0)
    image = np.zeros((100, 200), dtype=np.uint8)

    resized, final_dpi = preprocessing.normalize_resolution(image, source_dpi=150.0)

    assert final_dpi == 300.0
    assert resized.shape == (200, 400)  # source_dpi 150 -> target 300 is a 2x scale


def test_pathological_image_triggers_osd_fallback(monkeypatch: pytest.MonkeyPatch):
    calls = {"count": 0}

    def fake_osd(_pil_image):
        calls["count"] += 1
        return (
            "Page number: 0\nOrientation in degrees: 0\nRotate: 0\n"
            "Orientation confidence: 0.00\nScript: Latin\nScript confidence: 0.00\n"
        )

    monkeypatch.setattr(preprocessing.pytesseract, "image_to_osd", fake_osd)

    blank = np.full((200, 200), 255, dtype=np.uint8)  # no text mass at all
    result = preprocessing.estimate_skew_angle(blank)

    assert result.used_osd_fallback is True
    assert calls["count"] == 1
