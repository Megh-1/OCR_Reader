"""
Module 2 page detection: finding a photographed page's corners, and - just as
important - giving up (returning None, warping nothing) whenever the evidence
is not convincing.

Every image here is built in the test, so nothing depends on tests/fixtures.
The numbers are provisional: they pin behaviour measured on synthetic
composites of one page, not on real camera photos.
"""

import cv2
import numpy as np

from app.modules.preprocessing import decode_image, detect_document_quad, preprocess_image
from tests.test_deskew import png

PW, PH = 850, 1100  # the flat page
MARGIN = 0.28  # desk visible around the page, as a fraction of its size
DESK = (78, 105, 130)  # BGR, a dark blue-grey desk


def flat_page() -> np.ndarray:
    """A white page with its OWN printed border - the trap a scan falls into -
    and rows of ink so it isn't blank.

    The amount of ink is not incidental. The border-contrast gate compares mean
    brightness inside and outside a candidate outline, so a page that is inky
    enough passes it on ink alone: the real waybill's ink deficit (255 minus its
    mean gray) is ~18 and its own border measures a gap of 9-22 against a gate
    of 25, while this page at 3.6x that ink (deficit 63) was accepted as a
    photographed page. Keep it near the real form's density.
    """
    page = np.full((PH, PW, 3), 255, np.uint8)
    cv2.rectangle(page, (30, 30), (PW - 30, PH - 30), (0, 0, 0), 3)
    for i, y in enumerate(range(120, PH - 120, 80)):
        cv2.rectangle(page, (80, y), (PW - 80 - 60 * (i % 3), y + 8), (30, 30, 30), -1)
    return page


def test_fixture_page_has_the_ink_of_a_real_form():
    assert 10 < 255 - flat_page().mean() < 22


def photograph(
    corner_offsets=((0, 0), (0, 0), (0, 0), (0, 0)),
    desk=DESK,
    crop=None,
    seed=0,
) -> tuple[np.ndarray, np.ndarray]:
    """The page laid on a noisy desk and perspective-warped by moving its
    corners by `corner_offsets` (TL, TR, BR, BL). Returns the photo and the
    page's true corners in it."""
    cw, ch = int(PW * (1 + 2 * MARGIN)), int(PH * (1 + 2 * MARGIN))
    rng = np.random.default_rng(seed)
    canvas = np.clip(np.full((ch, cw, 3), desk, np.uint8) + rng.normal(0, 8, (ch, cw, 3)), 0, 255).astype(np.uint8)
    ox, oy = int(PW * MARGIN), int(PH * MARGIN)
    source = np.array([[0, 0], [PW - 1, 0], [PW - 1, PH - 1], [0, PH - 1]], "float32")
    target = source + np.array([ox, oy], "float32") + np.array(corner_offsets, "float32")
    matrix = cv2.getPerspectiveTransform(source, target)
    warped = cv2.warpPerspective(flat_page(), matrix, (cw, ch))
    mask = cv2.warpPerspective(np.full((PH, PW), 255, np.uint8), matrix, (cw, ch))
    canvas[mask > 0] = warped[mask > 0]
    corners = target.copy()
    if crop:
        x0, y0, x1, y1 = crop(ox, oy, cw, ch)
        canvas = canvas[y0:y1, x0:x1]
        corners -= np.array([x0, y0], "float32")
    return canvas, corners


def gray(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def worst_corner_error(found: np.ndarray, truth: np.ndarray) -> float:
    return max(min(np.linalg.norm(t - f) for f in found) for t in truth)


KEYSTONE = ((60, 0), (-60, 0), (0, 0), (0, 0))  # the top edge pulled in: a page shot from below


def test_finds_the_page_on_a_contrasting_desk():
    photo, truth = photograph(KEYSTONE)
    quad = detect_document_quad(gray(photo))
    assert quad is not None
    # ~1% of the page width; the outline is a few px generous (edge dilation), never wildly off.
    assert worst_corner_error(quad, truth) < 0.02 * PW


def test_keystoned_page_is_flattened_by_the_pipeline():
    photo, _ = photograph(KEYSTONE)
    processed = preprocess_image(png(gray(photo)))
    assert processed.document_detected
    # The warp is what straightens it; the skew estimator has nothing to add.
    assert processed.deskew_method == "document_detected"


def test_low_contrast_desk_gives_no_quad():
    """A page on a near-white desk has no edge worth trusting - refuse to warp."""
    photo, _ = photograph(KEYSTONE, desk=(232, 234, 236))
    assert detect_document_quad(gray(photo)) is None
    assert not preprocess_image(png(gray(photo))).document_detected


def test_partial_frame_gives_no_quad():
    """Part of the page is out of frame, so there is no four-cornered outline to find."""
    photo, _ = photograph(
        crop=lambda ox, oy, cw, ch: (ox + int(0.16 * PW), 0, cw, oy + int(0.92 * PH)),
    )
    assert detect_document_quad(gray(photo)) is None


def test_page_filling_the_frame_is_left_alone():
    """A scan or a PDF render: the only rectangle is the form's own printed border,
    which is not the page edge. Warping to it would crop the content away."""
    page = flat_page()
    assert detect_document_quad(gray(page)) is None
    processed = preprocess_image(png(gray(page)))
    assert not processed.document_detected


def test_tiny_page_in_a_big_frame_gives_no_quad():
    """Below the area floor a four-sided outline is far more likely to be clutter than the page."""
    photo, _ = photograph()
    small = cv2.copyMakeBorder(photo, *(int(photo.shape[0] * 1.5),) * 2, *(int(photo.shape[1] * 1.5),) * 2,
                               cv2.BORDER_REPLICATE)
    assert detect_document_quad(gray(small)) is None
