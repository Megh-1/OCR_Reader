"""
Module 2 page detection: finding a photographed page's corners, and - just as
important - giving up (returning None, warping nothing) whenever the evidence
is not convincing.

Every image here is built in the test, so nothing depends on tests/fixtures.
The numbers are provisional: they pin behaviour measured on synthetic
composites of one page, not on real camera photos.

The fixture page comes in two ink densities on purpose. "light" matches the
real waybill (ink deficit, 255 minus mean gray, ~18); "dense" is 3.6x that. The
border gate used to compare mean brightness over the whole inside with the whole
outside, which on a page whose printed border is the candidate measures ink, so
a dense page passed as "photographed". Every behaviour is asserted at both
densities so that cannot come back.
"""

import cv2
import numpy as np
import pytest

from app.modules.preprocessing import border_contrast, detect_document_quad, preprocess_image
from tests.test_deskew import png

PW, PH = 850, 1100  # the flat page
MARGIN = 0.28  # desk visible around the page, as a fraction of its size
DESK = (78, 105, 130)  # BGR, a dark blue-grey desk
NEAR_WHITE_DESK = (232, 234, 236)

INK = pytest.mark.parametrize("ink", ["light", "dense"])


def flat_page(ink: str = "light") -> np.ndarray:
    """A white page with its OWN printed border - the trap a scan falls into -
    and rows of ink so it isn't blank."""
    page = np.full((PH, PW, 3), 255, np.uint8)
    cv2.rectangle(page, (30, 30), (PW - 30, PH - 30), (0, 0, 0), 3)
    pitch, height = (80, 8) if ink == "light" else (110, 45)
    for i, y in enumerate(range(120, PH - 120, pitch)):
        cv2.rectangle(page, (80, y), (PW - 80 - (60 if ink == "light" else 40) * (i % 3), y + height), (30, 30, 30), -1)
    return page


def photograph(
    corner_offsets=((0, 0), (0, 0), (0, 0), (0, 0)),
    desk=DESK,
    crop=None,
    seed=0,
    ink="light",
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
    warped = cv2.warpPerspective(flat_page(ink), matrix, (cw, ch))
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


def ink_deficit(ink: str) -> float:
    return 255 - flat_page(ink).mean()


def test_the_two_fixture_densities_bracket_the_real_form():
    assert 10 < ink_deficit("light") < 22  # the real waybill: ~18
    assert ink_deficit("dense") > 50


@INK
def test_finds_the_page_on_a_contrasting_desk(ink):
    photo, truth = photograph(KEYSTONE, ink=ink)
    quad = detect_document_quad(gray(photo))
    assert quad is not None
    # ~1% of the page width; the outline is a few px generous (edge dilation), never wildly off.
    assert worst_corner_error(quad, truth) < 0.02 * PW


@INK
def test_keystoned_page_is_flattened_by_the_pipeline(ink):
    photo, _ = photograph(KEYSTONE, ink=ink)
    processed = preprocess_image(png(gray(photo)))
    assert processed.document_detected
    # The warp is what straightens it; the skew estimator has nothing to add.
    assert processed.deskew_method == "document_detected"


@INK
def test_low_contrast_desk_gives_no_quad(ink):
    """A page on a near-white desk has no edge worth trusting - refuse to warp.
    With the old whole-page gate the dense page instead got a WRONG quad here:
    its own printed border, admitted on ink."""
    photo, _ = photograph(KEYSTONE, desk=NEAR_WHITE_DESK, ink=ink)
    assert detect_document_quad(gray(photo)) is None
    assert not preprocess_image(png(gray(photo))).document_detected


@INK
def test_partial_frame_gives_no_quad(ink):
    """Part of the page is out of frame, so there is no four-cornered outline to find."""
    photo, _ = photograph(
        crop=lambda ox, oy, cw, ch: (ox + int(0.16 * PW), 0, cw, oy + int(0.92 * PH)),
        ink=ink,
    )
    assert detect_document_quad(gray(photo)) is None


@INK
def test_page_filling_the_frame_is_left_alone_however_inky_it_is(ink):
    """A scan or a PDF render: the only rectangle is the form's own printed border,
    which is not the page edge. Warping to it would crop the content away. The dense
    page is the regression: the old gate read its ink as an edge and accepted it."""
    page = flat_page(ink)
    assert detect_document_quad(gray(page)) is None
    assert not preprocess_image(png(gray(page))).document_detected


@INK
def test_tiny_page_in_a_big_frame_gives_no_quad(ink):
    """Below the area floor a four-sided outline is far more likely to be clutter than the page."""
    photo, _ = photograph(ink=ink)
    small = cv2.copyMakeBorder(photo, *(int(photo.shape[0] * 1.5),) * 2, *(int(photo.shape[1] * 1.5),) * 2,
                               cv2.BORDER_REPLICATE)
    assert detect_document_quad(gray(small)) is None


def test_the_measure_reads_the_edge_not_the_ink():
    """The same outline on the same desk reads the same contrast whether the page is sparse or dense."""
    readings = {}
    for ink in ("light", "dense"):
        photo, corners = photograph(ink=ink)
        readings[ink] = border_contrast(gray(photo), corners)
    assert min(readings.values()) > 60  # a real edge against a dark desk
    assert abs(readings["light"] - readings["dense"]) <= 5


@INK
def test_a_printed_border_has_paper_on_both_sides(ink):
    """The page fills the frame, so the outline is the form's own border - and there is
    only paper either side of it. (With the outline hugging the frame there is no outside
    band at all, which also reads 0.0: no evidence, no page.)"""
    page = gray(flat_page(ink))
    printed_border = np.array([[30, 30], [PW - 30, 30], [PW - 30, PH - 30], [30, PH - 30]], "float32")
    assert border_contrast(page, printed_border) < 5


def test_an_outline_touching_the_frame_all_round_has_no_outside_to_judge():
    everything = np.array([[0, 0], [PW - 1, 0], [PW - 1, PH - 1], [0, PH - 1]], "float32")
    assert border_contrast(gray(flat_page("light")), everything) == 0.0
