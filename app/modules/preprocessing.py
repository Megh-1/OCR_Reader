"""
Module 2 — Preprocessing.

Deterministically cleans a raw page image (as produced by Module 1) before
anything downstream tries to read it. Knows nothing about document types,
templates, or fields — its only job is: upright, denoised, contrast-boosted,
resolution-normalized.

Skew angle convention
----------------------
All angles here are in degrees, measured in image/array coordinates (x right,
y down — the OpenCV/numpy convention, not standard math Cartesian), normalized
to the range (-45, 45]. A POSITIVE angle means the page content is rotated
CLOCKWISE as displayed (text slopes downward left-to-right) and needs a
counter-clockwise correction; a NEGATIVE angle means the opposite.

The angle is computed from cv2.boxPoints() corner geometry rather than trusted
from cv2.minAreaRect()'s raw `angle` field, because that field's sign/range
convention changed between OpenCV versions (pre- vs post-4.5.1). Trusting it
blindly is exactly the kind of silent bug that flips every rotation correction
in the wrong direction without ever raising an error.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import pytesseract
from PIL import Image

from app.config import (
    ADAPTIVE_THRESH_BLOCK_SIZE,
    ADAPTIVE_THRESH_C,
    DEFAULT_SOURCE_DPI,
    DENOISE_STRENGTH,
    MAX_UPSCALE_FACTOR,
    MIN_CONTOUR_AREA_FRACTION,
    SKEW_ANGLE_NOOP_THRESHOLD,
    SKEW_DILATE_ITERATIONS,
    SKEW_DILATE_KERNEL_SIZE,
    TARGET_DPI,
)


@dataclass
class SkewEstimate:
    angle: float
    used_osd_fallback: bool


@dataclass
class ProcessedImage:
    """
    Two renderings of the same page, at identical dimensions so one template's
    relative bboxes map to the same region in either.

    They exist separately because anchor verification and field extraction
    want opposite things from an image, which measurement on a real photo made
    unavoidable: binarization is the only reason header text in a design-heavy
    title band is detectable at all (grayscale scored 0 on the anchor), but it
    shreds the stroke detail character recognition depends on (two of three
    fields misread, one at confidence 39 - both read exactly right, at
    confidence 84-88, off the grayscale). Serving both from one image means
    picking which module to break.
    """

    image_bytes: bytes  # binarized - Module 5 anchor verification
    field_image_bytes: bytes  # grayscale, full stroke detail - Module 6 field extraction
    skew_angle_corrected: float
    osd_fallback_used: bool
    final_dpi: float
    final_size: tuple[int, int]  # (width, height)


def decode_image(image_bytes: bytes) -> np.ndarray:
    pil_image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    return cv2.cvtColor(np.array(pil_image), cv2.COLOR_RGB2BGR)


def encode_image(image: np.ndarray) -> bytes:
    if image.ndim == 2:
        pil_image = Image.fromarray(image, mode="L")
    else:
        pil_image = Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    out = io.BytesIO()
    pil_image.save(out, format="PNG")
    return out.getvalue()


def _normalize_rect_angle(rect: tuple) -> float:
    box = cv2.boxPoints(rect)
    edge1 = box[1] - box[0]
    edge2 = box[2] - box[1]
    dx, dy = edge1 if np.hypot(*edge1) >= np.hypot(*edge2) else edge2

    angle = math.degrees(math.atan2(dy, dx))
    angle %= 180
    if angle > 90:
        angle -= 180
    if angle > 45:
        angle -= 90
    elif angle <= -45:
        angle += 90
    return angle


def estimate_skew_via_osd(gray: np.ndarray) -> float:
    """
    Fallback skew estimate using Tesseract's orientation/script detection.
    Returns 0.0 if OSD can't determine an angle (e.g. too little text) or the
    Tesseract binary isn't available.

    Tesseract OSD only reports coarse, cardinal rotation (0/90/180/270), not a
    fine skew angle — it's a fallback for "wildly wrong" cases, not a
    replacement for the contour method's precision.
    """
    try:
        osd = pytesseract.image_to_osd(Image.fromarray(gray))
    except Exception:
        return 0.0

    match = re.search(r"Rotate: (\d+)", osd)
    if not match:
        return 0.0

    rotate = int(match.group(1))
    if rotate > 180:
        rotate -= 360
    return float(rotate)


def estimate_skew_angle(gray: np.ndarray) -> SkewEstimate:
    """
    Primary: threshold + dilate to merge text into one mass, take its largest
    contour, and read the skew off that contour's minAreaRect. Falls back to
    Tesseract OSD when no contour carries enough of the page to be trusted.
    """
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    kernel = np.ones((SKEW_DILATE_KERNEL_SIZE, SKEW_DILATE_KERNEL_SIZE), np.uint8)
    dilated = cv2.dilate(binary, kernel, iterations=SKEW_DILATE_ITERATIONS)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page_area = gray.shape[0] * gray.shape[1]

    largest = max(contours, key=cv2.contourArea) if contours else None
    area_fraction = (cv2.contourArea(largest) / page_area) if largest is not None else 0.0

    if largest is None or area_fraction < MIN_CONTOUR_AREA_FRACTION:
        return SkewEstimate(angle=estimate_skew_via_osd(gray), used_osd_fallback=True)

    rect = cv2.minAreaRect(largest)
    return SkewEstimate(angle=_normalize_rect_angle(rect), used_osd_fallback=False)


def correct_rotation(image: np.ndarray, angle: float) -> np.ndarray:
    """
    Rotates `image` to cancel a skew of `angle` degrees under this module's
    clockwise-positive convention (see module docstring). Expands the canvas
    so corners aren't clipped, padding new area with white.
    """
    if abs(angle) < SKEW_ANGLE_NOOP_THRESHOLD:
        return image

    (h, w) = image.shape[:2]
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)

    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    new_w = int((h * sin) + (w * cos))
    new_h = int((h * cos) + (w * sin))
    matrix[0, 2] += (new_w / 2) - center[0]
    matrix[1, 2] += (new_h / 2) - center[1]

    border_value = 255 if image.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(
        image,
        matrix,
        (new_w, new_h),
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_value,
    )


def denoise_and_enhance(gray: np.ndarray) -> np.ndarray:
    denoised = cv2.fastNlMeansDenoising(gray, h=DENOISE_STRENGTH)
    return cv2.adaptiveThreshold(
        denoised,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        ADAPTIVE_THRESH_BLOCK_SIZE,
        ADAPTIVE_THRESH_C,
    )


def normalize_resolution(image: np.ndarray, source_dpi: float) -> tuple[np.ndarray, float]:
    """
    Rescales `image` from `source_dpi` toward TARGET_DPI, capped at
    MAX_UPSCALE_FACTOR regardless of what source_dpi claims to be - see
    config.MAX_UPSCALE_FACTOR: real phone-camera photos near-universally
    embed a meaningless "72 DPI" tag, and trusting that verbatim on an
    already-high-resolution photo computes an upscale large enough to trip
    Pillow's decompression-bomb guard before the image reaches Module 5.
    This is the highest-stakes step in the module: every downstream
    template's field boxes are relative percentages of page dimensions, so
    an inconsistent scale factor here breaks every template silently,
    later, downstream. The returned DPI reflects what was actually
    achieved, not the nominal target, so it stays honest when the cap
    kicks in.
    """
    scale = min(TARGET_DPI / source_dpi, MAX_UPSCALE_FACTOR)
    interpolation = cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA
    resized = cv2.resize(image, None, fx=scale, fy=scale, interpolation=interpolation)
    return resized, source_dpi * scale


def _infer_source_dpi(pil_image: Image.Image, explicit_dpi: Optional[float]) -> float:
    if explicit_dpi is not None:
        return explicit_dpi
    dpi_info = pil_image.info.get("dpi")
    if dpi_info:
        return float(dpi_info[0])
    return DEFAULT_SOURCE_DPI


def preprocess_image(image_bytes: bytes, source_dpi: Optional[float] = None) -> ProcessedImage:
    """
    Entry point handed off from Module 1's page image bytes. Returns the
    cleaned image plus metadata (skew angle corrected, whether the OSD
    fallback fired, final resolution) that Module 9 will eventually want when
    debugging a bad extraction — never thrown away.

    Module 5 isn't built yet; this is the clean handoff point for it.
    """
    pil_source = Image.open(io.BytesIO(image_bytes))
    dpi = _infer_source_dpi(pil_source, source_dpi)

    bgr = decode_image(image_bytes)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    skew = estimate_skew_angle(gray)
    rotated = correct_rotation(bgr, skew.angle)
    rotated_gray = cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY)

    cleaned = denoise_and_enhance(rotated_gray)
    resized, final_dpi = normalize_resolution(cleaned, dpi)

    # Same rotation, same scale factor, same source - so both renderings come
    # out at identical dimensions and a template's relative bboxes address the
    # same pixels in either one.
    field_resized, _ = normalize_resolution(rotated_gray, dpi)

    return ProcessedImage(
        image_bytes=encode_image(resized),
        field_image_bytes=encode_image(field_resized),
        skew_angle_corrected=skew.angle,
        osd_fallback_used=skew.used_osd_fallback,
        final_dpi=final_dpi,
        final_size=(resized.shape[1], resized.shape[0]),
    )
