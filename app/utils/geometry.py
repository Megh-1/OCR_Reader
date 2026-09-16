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
