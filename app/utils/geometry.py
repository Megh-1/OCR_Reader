"""
Shared geometry utilities. A template's bbox fields are relative [x1, y1,
x2, y2] coordinates (0.0-1.0) - this is the one place that converts them
into absolute pixel coordinates for a specific image. Both anchor
verification (Module 5) and field extraction (Module 6) crop regions out of
an image using a template's bbox, so this lives here once rather than being
implemented twice and risking the two copies drifting apart.
"""

from __future__ import annotations


def relative_bbox_to_pixels(
    bbox: tuple[float, float, float, float], image_width: int, image_height: int
) -> tuple[int, int, int, int]:
    """
    Converts a [x1, y1, x2, y2] relative bbox (0.0-1.0, as stored in a
    template) into an absolute pixel (left, top, right, bottom) bbox for an
    image of the given dimensions - the inverse of the px->% conversion the
    template designer UI performs when a box is drawn.
    """
    x1, y1, x2, y2 = bbox
    return (
        round(x1 * image_width),
        round(y1 * image_height),
        round(x2 * image_width),
        round(y2 * image_height),
    )


def clamped_crop_box(
    bbox: tuple[float, float, float, float], image_width: int, image_height: int, margin: int
) -> tuple[int, int, int, int]:
    """
    relative_bbox_to_pixels() + a fixed margin on each side, then clamped to
    the image's actual bounds. A box near an edge, expanded by the margin,
    can extend past (0,0)-(width,height) - crop() on out-of-bounds
    coordinates doesn't raise, but silently reading past the real image
    edge is exactly the kind of thing that must never happen unnoticed.

    Shared by anchor cropping (Module 5) and field cropping (Module 6) -
    both need the same margin-then-clamp behavior, just calibrated with
    different margin sizes for their different crop-content characteristics
    (a wide anchor line needs more slack than a small field value - see
    each caller's own margin constant for why).
    """
    left, top, right, bottom = relative_bbox_to_pixels(bbox, image_width, image_height)
    left = max(0, left - margin)
    top = max(0, top - margin)
    right = min(image_width, right + margin)
    bottom = min(image_height, bottom + margin)
    return (left, top, right, bottom)


def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, v))


def pixels_to_relative_bbox(
    bbox_px: tuple[float, float, float, float], image_width: int, image_height: int
) -> tuple[float, float, float, float]:
    """
    The other direction: an absolute pixel (left, top, right, bottom) bbox
    for an image of the given dimensions, converted to a relative [x1, y1,
    x2, y2] bbox (0.0-1.0) for storing in a template. Until now only the
    frontend (template designer UI, in JS) had this direction - this is the
    server-side equivalent, for anything that needs to build a template
    from detected pixel coordinates (e.g. an OCR-assisted authoring tool)
    without going through the browser.
    """
    left, top, right, bottom = bbox_px
    return (
        _clamp01(left / image_width),
        _clamp01(top / image_height),
        _clamp01(right / image_width),
        _clamp01(bottom / image_height),
    )
