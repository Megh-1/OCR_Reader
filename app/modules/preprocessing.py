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
    CLAHE_CLIP_LIMIT,
    CLAHE_TILE_GRID,
    DEFAULT_SOURCE_DPI,
    DENOISE_STRENGTH,
    DOC_DETECT_APPROX_EPSILON,
    DOC_DETECT_MAX_AREA_FRACTION,
    DOC_DETECT_MAX_ASPECT,
    DOC_DETECT_MIN_AREA_FRACTION,
    DOC_DETECT_MIN_BORDER_CONTRAST,
    DOC_DETECT_MIN_ASPECT,
    DOC_DETECT_WORKING_WIDTH,
    MAX_UPSCALE_FACTOR,
    MIN_CONTOUR_AREA_FRACTION,
    OCR_TARGET_LONG_EDGE_PX,
    RULE_DETECT_BLOCK_SIZE,
    RULE_DILATE_PX,
    RULE_MIN_LENGTH_DIVISOR,
    SKEW_ANGLE_NOOP_THRESHOLD,
    SKEW_DILATE_ITERATIONS,
    SKEW_DILATE_KERNEL_SIZE,
    TARGET_DPI,
)


@dataclass
class SkewEstimate:
    angle: float
    used_osd_fallback: bool


def _order_quad_corners(points: np.ndarray) -> np.ndarray:
    """
    Orders four corners as top-left, top-right, bottom-right, bottom-left.

    Uses coordinate sums and differences rather than angles: the top-left
    corner has the smallest x+y and the bottom-right the largest, while the
    top-right has the smallest y-x and the bottom-left the largest. That
    holds for any rotation up to +/-45 degrees, which is the range a
    photographed page realistically falls in, and needs no trigonometry.
    """
    points = points.reshape(4, 2).astype("float32")
    ordered = np.zeros((4, 2), dtype="float32")

    coordinate_sum = points.sum(axis=1)
    ordered[0] = points[np.argmin(coordinate_sum)]  # top-left
    ordered[2] = points[np.argmax(coordinate_sum)]  # bottom-right

    coordinate_diff = np.diff(points, axis=1).ravel()  # y - x
    ordered[1] = points[np.argmin(coordinate_diff)]  # top-right
    ordered[3] = points[np.argmax(coordinate_diff)]  # bottom-left
    return ordered


def detect_document_quad(gray: np.ndarray) -> Optional[np.ndarray]:
    """
    Finds the page's four corners in a photo, or None when nothing
    convincing is there.

    None is a normal, frequent answer - a scan, a PDF render, or a photo
    cropped tight to the page all legitimately have no visible document
    border. Returning None means "leave the image alone", which is why every
    acceptance check below is a reason to give up rather than to guess.
    """
    height, width = gray.shape[:2]
    scale = DOC_DETECT_WORKING_WIDTH / width
    if scale < 1.0:
        working = cv2.resize(gray, (DOC_DETECT_WORKING_WIDTH, round(height * scale)), interpolation=cv2.INTER_AREA)
    else:
        working, scale = gray, 1.0

    blurred = cv2.GaussianBlur(working, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    # Close gaps where a page edge crosses a low-contrast patch of background,
    # so the border survives as one contour instead of several fragments.
    edges = cv2.dilate(edges, np.ones((5, 5), np.uint8), iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    working_area = working.shape[0] * working.shape[1]

    for contour in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        approximation = cv2.approxPolyDP(contour, DOC_DETECT_APPROX_EPSILON * cv2.arcLength(contour, True), True)
        if len(approximation) != 4 or not cv2.isContourConvex(approximation):
            continue

        area_fraction = cv2.contourArea(approximation) / working_area
        if not (DOC_DETECT_MIN_AREA_FRACTION <= area_fraction <= DOC_DETECT_MAX_AREA_FRACTION):
            continue

        corners = _order_quad_corners(approximation)
        edge_width = max(np.linalg.norm(corners[1] - corners[0]), np.linalg.norm(corners[2] - corners[3]))
        edge_height = max(np.linalg.norm(corners[3] - corners[0]), np.linalg.norm(corners[2] - corners[1]))
        if edge_height == 0:
            continue
        if not (DOC_DETECT_MIN_ASPECT <= edge_width / edge_height <= DOC_DETECT_MAX_ASPECT):
            continue

        if not _has_distinct_border(working, approximation):
            continue

        return corners / scale  # back into full-resolution coordinates

    return None


def _has_distinct_border(gray: np.ndarray, quad: np.ndarray) -> bool:
    """
    Requires what's outside the candidate quad to look materially different
    from what's inside it.

    Without this check the strongest rectangle in a scan or a PDF render is
    the form's OWN printed border, which is a perfectly valid convex quad of
    plausible size - accepting it warps the page to the form frame and crops
    the content outside it away. A real photograph has the page against
    something else (a desk, a table), so a genuine document edge always has
    contrast across it; a printed rule on white paper does not.
    """
    inside_mask = np.zeros(gray.shape[:2], np.uint8)
    cv2.fillConvexPoly(inside_mask, quad.reshape(4, 2).astype(np.int32), 255)
    outside_mask = cv2.bitwise_not(inside_mask)

    if cv2.countNonZero(outside_mask) == 0:
        return False

    inside_mean = cv2.mean(gray, mask=inside_mask)[0]
    outside_mean = cv2.mean(gray, mask=outside_mask)[0]
    return abs(inside_mean - outside_mean) >= DOC_DETECT_MIN_BORDER_CONTRAST


def four_point_transform(
    image: np.ndarray, corners: np.ndarray, target_aspect: Optional[float] = None
) -> np.ndarray:
    """
    Warps the quadrilateral at `corners` onto a flat rectangle - correcting
    rotation, perspective and framing in one operation, and cropping away
    everything outside the page as a side effect.

    Output dimensions come from the longest opposing edges, so a page
    photographed at an angle keeps roughly its true proportions instead of
    being squashed toward the foreshortened side.

    `target_aspect` (width/height) replaces that estimate with the page's
    real ratio when the caller knows it. Measured edges are foreshortened by
    the very perspective being corrected, so the estimate lands a few percent
    out - harmless for reading the page, but every template bbox is a
    percentage of page width, so a few percent of horizontal error is enough
    to slide a field box off its cell. Pixel count is preserved, so only the
    ratio changes and no detail is scaled away.
    """
    top_left, top_right, bottom_right, bottom_left = corners
    output_width = round(max(np.linalg.norm(bottom_right - bottom_left), np.linalg.norm(top_right - top_left)))
    output_height = round(max(np.linalg.norm(top_right - bottom_right), np.linalg.norm(top_left - bottom_left)))

    if target_aspect is not None:
        output_height = round(math.sqrt(output_width * output_height / target_aspect))
        output_width = round(output_height * target_aspect)

    destination = np.array(
        [[0, 0], [output_width - 1, 0], [output_width - 1, output_height - 1], [0, output_height - 1]],
        dtype="float32",
    )
    matrix = cv2.getPerspectiveTransform(corners, destination)
    return cv2.warpPerspective(image, matrix, (output_width, output_height))


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
    document_detected: bool = False  # whether a page boundary was found and flattened


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


def remove_ruled_lines(gray: np.ndarray) -> np.ndarray:
    """
    Erases the printed rules of a ruled form, leaving the text.

    On a grid form - an air waybill, an invoice table, a passbook - every
    field sits inside a ruled cell, so every crop catches its borders and
    Tesseract reads them as characters: a left-hand rule becomes "|", an
    underline becomes "_", a corner becomes "J", and "65" inside a box comes
    back as "Jos". Measured on a real air waybill, this was the single
    largest source of wrong field values, ahead of every image-quality factor.

    Lines are found by morphological opening with kernels far longer than any
    glyph, so only strokes that run a substantial fraction of the page width
    or height survive - which is why this erases rules without erasing text.
    It must run on the whole page, before any field is cropped, since a rule
    is only recognizable as a rule at its full length.
    """
    inverted = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, RULE_DETECT_BLOCK_SIZE, -2
    )

    height, width = gray.shape[:2]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(10, width // RULE_MIN_LENGTH_DIVISOR), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(10, height // RULE_MIN_LENGTH_DIVISOR)))

    rules = cv2.bitwise_or(
        cv2.morphologyEx(inverted, cv2.MORPH_OPEN, horizontal_kernel),
        cv2.morphologyEx(inverted, cv2.MORPH_OPEN, vertical_kernel),
    )
    # A rule's anti-aliased shoulders survive the opening; widening slightly
    # takes them too, otherwise a grey ghost of each line is left behind and
    # Tesseract still finds a stroke there.
    rules = cv2.dilate(rules, np.ones((RULE_DILATE_PX, RULE_DILATE_PX), np.uint8), iterations=1)

    cleaned = gray.copy()
    cleaned[rules > 0] = 255  # paint erased rules as page background
    return cleaned


def equalize_local_contrast(gray: np.ndarray) -> np.ndarray:
    """
    CLAHE, for the field-extraction rendering.

    Unlike adaptiveThreshold (which the anchor rendering needs), this keeps
    the image grayscale and keeps stroke detail intact - it only redistributes
    local contrast, so a dim or unevenly lit photo becomes legible without
    the glyph damage binarization causes. Standard clip/tile parameters, not
    values fitted to any one sample.
    """
    clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP_LIMIT, tileGridSize=(CLAHE_TILE_GRID, CLAHE_TILE_GRID))
    return clahe.apply(gray)


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

    # Second, independent ceiling on top of MAX_UPSCALE_FACTOR: that one
    # guards against an implausible source_dpi CLAIM, this one against the
    # resulting image being too large to read well regardless of how the
    # scale was arrived at (see config.OCR_TARGET_LONG_EDGE_PX for the
    # measurements). Clamped at 1.0 so this only ever declines to magnify -
    # it never shrinks an image that was captured at genuinely high detail.
    long_edge = max(image.shape[0], image.shape[1])
    if long_edge * scale > OCR_TARGET_LONG_EDGE_PX:
        scale = max(1.0, OCR_TARGET_LONG_EDGE_PX / long_edge)

    if scale == 1.0:
        return image, source_dpi

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


def preprocess_image(
    image_bytes: bytes,
    source_dpi: Optional[float] = None,
    target_aspect: Optional[float] = None,
    remove_rules: bool = False,
) -> ProcessedImage:
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

    # Locate and flatten the page first, when it's findable. Everything after
    # this point - and every template coordinate downstream - is relative to
    # the page rather than to however the photo happened to be framed.
    corners = detect_document_quad(gray)
    document_detected = corners is not None
    if document_detected:
        bgr = four_point_transform(bgr, corners, target_aspect)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # The warp already squared the page up, so residual skew is sub-degree at
    # most; running the estimator on a flattened page would mostly measure
    # noise in the text mass and rotate a correct image back out of true.
    skew = SkewEstimate(angle=0.0, used_osd_fallback=False) if document_detected else estimate_skew_angle(gray)
    rotated = correct_rotation(bgr, skew.angle)
    rotated_gray = cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY)

    cleaned = denoise_and_enhance(rotated_gray)
    resized, final_dpi = normalize_resolution(cleaned, dpi)

    # Same rotation, same scale factor, same source - so both renderings come
    # out at identical dimensions and a template's relative bboxes address the
    # same pixels in either one.
    field_source = remove_ruled_lines(rotated_gray) if remove_rules else rotated_gray
    field_resized, _ = normalize_resolution(equalize_local_contrast(field_source), dpi)

    return ProcessedImage(
        image_bytes=encode_image(resized),
        field_image_bytes=encode_image(field_resized),
        skew_angle_corrected=skew.angle,
        osd_fallback_used=skew.used_osd_fallback,
        final_dpi=final_dpi,
        final_size=(resized.shape[1], resized.shape[0]),
        document_detected=document_detected,
    )
