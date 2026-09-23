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
import logging
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
    DESKEW_MAX_ANGLE_CORRECTION,
    DOC_DETECT_APPROX_EPSILON,
    DOC_DETECT_BAND_SKIP_PX,
    DOC_DETECT_BAND_WIDTH_PX,
    DOC_DETECT_MAX_AREA_FRACTION,
    DOC_DETECT_MAX_ASPECT,
    DOC_DETECT_MIN_AREA_FRACTION,
    DOC_DETECT_MIN_BORDER_CONTRAST,
    DOC_DETECT_MIN_ASPECT,
    DOC_DETECT_WORKING_WIDTH,
    HOUGH_ANGLE_RESOLUTION_DEG,
    HOUGH_ANGLE_WINDOW_DEG,
    HOUGH_MAX_LINE_GAP_FRACTION,
    HOUGH_MIN_LINE_FRACTION,
    HOUGH_MIN_LINES,
    HOUGH_VOTE_THRESHOLD,
    HOUGH_WORKING_LONG_EDGE,
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

log = logging.getLogger(__name__)


@dataclass
class SkewEstimate:
    angle: float
    used_osd_fallback: bool
    method: str = "contour"  # "contour" | "hough" | "osd"


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


# Below this many pixels in either band there is no evidence about that side of the
# outline (the outline touches the frame all round, say) and the gate refuses. A
# degeneracy guard, not a tuned value.
_MIN_BAND_PIXELS = 100


def border_contrast(gray: np.ndarray, quad: np.ndarray) -> float:
    """
    How different the paper looks from what it lies on, measured AT the outline:
    the absolute gap between the median brightness of a thin band just inside
    the outline and a matching band just outside it.

    Both bands hug the same edge, so what is on the page does not enter into it.
    The measure this replaces compared the mean over the whole inside with the
    mean over the whole outside; on a page whose printed border is the candidate
    that is really the page's INK DENSITY, so an inky enough form "had a border"
    on ink alone. Medians also shrug off a text row or a shadow crossing a band.

    Each band skips DOC_DETECT_BAND_SKIP_PX beside the outline. The outline is
    the outer boundary of DILATED Canny edges (see detect_document_quad), so it
    sits about 4 px outside the true edge, and the edge itself is a few pixels
    wide; sampling from the outline outward or inward would put the transition
    inside a band and dilute the gap.

    Returns 0.0 when either band is too thin to judge, so the caller refuses.
    """
    polygon = quad.reshape(4, 2).astype(np.int32)
    inside = np.zeros(gray.shape[:2], np.uint8)
    cv2.fillConvexPoly(inside, polygon, 255)

    # Distance of each pixel to the outline, from either side.
    inward = cv2.distanceTransform(inside, cv2.DIST_L2, 3)
    outward = cv2.distanceTransform(cv2.bitwise_not(inside), cv2.DIST_L2, 3)
    near, far = DOC_DETECT_BAND_SKIP_PX, DOC_DETECT_BAND_SKIP_PX + DOC_DETECT_BAND_WIDTH_PX
    inner_band = (inward > near) & (inward <= far)
    outer_band = (outward > near) & (outward <= far)
    if inner_band.sum() < _MIN_BAND_PIXELS or outer_band.sum() < _MIN_BAND_PIXELS:
        return 0.0

    return abs(float(np.median(gray[inner_band])) - float(np.median(gray[outer_band])))


def _has_distinct_border(gray: np.ndarray, quad: np.ndarray) -> bool:
    """
    Requires the candidate outline to be an edge between two different
    surfaces, not a mark on one.

    Without this check the strongest rectangle in a scan or a PDF render is
    the form's OWN printed border, which is a perfectly valid convex quad of
    plausible size - accepting it warps the page to the form frame and crops
    the content outside it away. A real photograph has the page against
    something else (a desk, a table), so a genuine document edge has contrast
    across it; a printed rule on white paper has paper on both sides of it.
    """
    return border_contrast(gray, quad) >= DOC_DETECT_MIN_BORDER_CONTRAST


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
    skew_angle_detected: float = 0.0  # what the estimator measured, even when it was not applied
    deskew_method: str = "none"  # contour | hough | osd | document_detected | disabled
    deskew_applied: bool = False  # a rotation was actually performed


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


def estimate_skew_via_hough(gray: np.ndarray) -> Optional[float]:
    """
    Skew from the long straight segments on the page - a form's printed rules,
    the baselines of text lines - or None when there is not enough of them to
    trust.

    Every segment's angle is folded modulo 90 degrees, so a horizontal rule and
    a vertical one both vote for the same skew; the answer is the
    length-weighted median, which one stray diagonal cannot move. Segments
    further than HOUGH_ANGLE_WINDOW_DEG from an axis are ignored as not being
    skew evidence at all.

    Sign convention matches this module: positive = content rotated
    clockwise as displayed.
    """
    scale = min(1.0, HOUGH_WORKING_LONG_EDGE / max(gray.shape[:2]))
    working = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1.0 else gray
    edges = cv2.Canny(cv2.GaussianBlur(working, (3, 3), 0), 50, 150)

    width = working.shape[1]
    segments = cv2.HoughLinesP(
        edges,
        1,
        math.radians(HOUGH_ANGLE_RESOLUTION_DEG),
        HOUGH_VOTE_THRESHOLD,
        minLineLength=int(width * HOUGH_MIN_LINE_FRACTION),
        maxLineGap=max(1, int(width * HOUGH_MAX_LINE_GAP_FRACTION)),
    )
    if segments is None:
        return None

    deviations, lengths = [], []
    for x1, y1, x2, y2 in segments.reshape(-1, 4):
        deviation = (math.degrees(math.atan2(y2 - y1, x2 - x1)) + 45.0) % 90.0 - 45.0
        if abs(deviation) <= HOUGH_ANGLE_WINDOW_DEG:
            deviations.append(deviation)
            lengths.append(math.hypot(x2 - x1, y2 - y1))
    if len(deviations) < HOUGH_MIN_LINES:
        return None

    order = np.argsort(deviations)
    deviations_sorted = np.asarray(deviations)[order]
    cumulative = np.cumsum(np.asarray(lengths)[order])
    return float(deviations_sorted[np.searchsorted(cumulative, cumulative[-1] / 2)])


def estimate_skew_angle(gray: np.ndarray) -> SkewEstimate:
    """
    Primary: threshold + dilate to merge text into one mass, take its largest
    contour, and read the skew off that contour's minAreaRect. When no contour
    carries enough of the page to be trusted, fall back to Hough line fitting,
    and only if that finds too little too, to Tesseract OSD.

    OSD is deliberately last: it reports orientation in 90-degree steps and
    cannot see a 1.5-degree skew at all (see config.DESKEW_MAX_ANGLE_CORRECTION
    for the measurement).
    """
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    kernel = np.ones((SKEW_DILATE_KERNEL_SIZE, SKEW_DILATE_KERNEL_SIZE), np.uint8)
    dilated = cv2.dilate(binary, kernel, iterations=SKEW_DILATE_ITERATIONS)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    page_area = gray.shape[0] * gray.shape[1]

    largest = max(contours, key=cv2.contourArea) if contours else None
    area_fraction = (cv2.contourArea(largest) / page_area) if largest is not None else 0.0

    if largest is None or area_fraction < MIN_CONTOUR_AREA_FRACTION:
        hough_angle = estimate_skew_via_hough(gray)
        if hough_angle is not None:
            return SkewEstimate(angle=hough_angle, used_osd_fallback=False, method="hough")
        return SkewEstimate(angle=estimate_skew_via_osd(gray), used_osd_fallback=True, method="osd")

    rect = cv2.minAreaRect(largest)
    return SkewEstimate(angle=_normalize_rect_angle(rect), used_osd_fallback=False, method="contour")


def correct_rotation(image: np.ndarray, angle: float, expand: bool = False) -> np.ndarray:
    """
    Rotates `image` to cancel a skew of `angle` degrees under this module's
    clockwise-positive convention (see module docstring), padding whatever the
    rotation uncovers with white.

    By default the canvas keeps its size and the page turns about its centre.
    Every template bbox is a percentage of the FRAME, so the frame has to stay
    the frame: growing the canvas to avoid clipping pads the page with white,
    which scales it down inside the image and shifts every field box off its
    cell. Measured on a synthetic scan skewed by only 1.5 degrees, with the
    angle recovered exactly: 8/29 fields with no deskew, 7/29 with the
    canvas-expanding correction, 25/29 with this. At 2.5 degrees the expanding
    version pushed the anchors out of range and the page was refused outright.

    The price is that corner content the rotation carries out of the frame is
    lost - a margin sliver at these angles, which is why the correction is
    capped (config.DESKEW_MAX_ANGLE_CORRECTION). `expand=True` restores the old
    behaviour for a caller that has no template coordinates to protect.
    """
    if abs(angle) < SKEW_ANGLE_NOOP_THRESHOLD:
        return image

    (h, w) = image.shape[:2]
    center = (w / 2, h / 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)

    out_w, out_h = w, h
    if expand:
        cos = abs(matrix[0, 0])
        sin = abs(matrix[0, 1])
        out_w = int((h * sin) + (w * cos))
        out_h = int((h * cos) + (w * sin))
        matrix[0, 2] += (out_w / 2) - center[0]
        matrix[1, 2] += (out_h / 2) - center[1]

    border_value = 255 if image.ndim == 2 else (255, 255, 255)
    return cv2.warpAffine(
        image,
        matrix,
        (out_w, out_h),
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
    deskew: bool = True,
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
    # `deskew=False` is for callers that know the page is upright - a text-native
    # PDF render - where estimating skew could only ever introduce error.
    detected_angle, applied_angle, osd_fallback = 0.0, 0.0, False
    if document_detected:
        deskew_method = "document_detected"
    elif not deskew:
        deskew_method = "disabled"
    else:
        skew = estimate_skew_angle(gray)
        detected_angle, deskew_method, osd_fallback = skew.angle, skew.method, skew.used_osd_fallback
        if abs(skew.angle) > DESKEW_MAX_ANGLE_CORRECTION:
            log.warning(
                "deskew skipped: detected %+.2f deg via %s exceeds DESKEW_MAX_ANGLE_CORRECTION (%.1f) - "
                "likely a genuinely rotated page, not scan skew; left as-is",
                skew.angle, skew.method, DESKEW_MAX_ANGLE_CORRECTION,
            )
        else:
            applied_angle = skew.angle
    rotated = correct_rotation(bgr, applied_angle)
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
        skew_angle_corrected=applied_angle,
        osd_fallback_used=osd_fallback,
        final_dpi=final_dpi,
        final_size=(resized.shape[1], resized.shape[0]),
        document_detected=document_detected,
        skew_angle_detected=detected_angle,
        deskew_method=deskew_method,
        deskew_applied=abs(applied_angle) >= SKEW_ANGLE_NOOP_THRESHOLD,
    )
