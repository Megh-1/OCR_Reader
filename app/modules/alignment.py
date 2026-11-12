"""
Page alignment: find where a template's printed words actually sit on this page, and move
every box by the shift and stretch that implies.

Template boxes are page fractions, which assumes the form lands at the same place on every page.
Real files break that: an A4 scan of a Letter form is ~7% shorter, a scan margin moves everything,
one printer's version of the form has a taller header. Without this, 15 of 16 real waybills were
refused because the anchor crop landed on blank paper.

The method is deliberately plain: OCR the whole page once, match the words to the template's
`landmarks` (printed words recorded from real documents), fit a shift and stretch per axis with
RANSAC so words that are data rather than form text are simply outvoted, then correct each box
by the median leftover of the landmarks nearest to it. No learned model.

Two rules keep it from making things worse:
  - It returns None, never a guess, when too few landmarks agree. The caller then behaves exactly
    as it did before alignment existed (boxes where they are stored).
  - The boxes that verify a page are the boxes that read it. A page that passes its anchors only
    at the unaligned position must not then be read at the aligned one, so Module 5 uses aligned
    boxes whenever an alignment exists, and refuses the page if they miss.

A form whose columns sit elsewhere (a different printer's version) is NOT fixed by this - the
shift is not uniform across it. It fails the anchors and is refused, which is the right answer: it
needs its own template.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output
from rapidfuzz import fuzz

from app.config import (
    ALIGN_LOCAL_NEIGHBOURS,
    ALIGN_LOCAL_MIN_NEIGHBOURS,
    ALIGN_LOCAL_RADIUS,
    ALIGN_MATCH_RATIO,
    ALIGN_MAX_CROSS,
    ALIGN_MIN_LANDMARK_FRACTION,
    ALIGN_MIN_LANDMARKS,
    ALIGN_MIN_WORD_CONF,
    ALIGN_MIN_WORD_LEN,
    ALIGN_OCR_PSM,
    ALIGN_RANSAC_ITERS,
    ALIGN_RANSAC_TOL,
    ALIGN_SCALE_RANGE,
)
from app.schemas.template import BBox, TemplateLandmark

log = logging.getLogger(__name__)

# Two reference words further apart than this on an axis cannot pin that axis' scale.
_MIN_SPREAD = 0.12
_RANSAC_SEED = 1  # fixed: the same page must always align the same way
_MIN_EXTENT = 0.002  # narrowest a mapped box may be, as a page fraction


@dataclass(frozen=True)
class PageWord:
    text: str  # lower-case letters only
    x: float  # left edge, fraction of page width
    y: float  # vertical centre, fraction of page height


def normalize_word(raw: str) -> str:
    return re.sub(r"[^a-z]", "", raw.lower())


def read_page_words(image: Image.Image) -> list[PageWord]:
    """One full-page OCR; the words usable as landmarks, positioned as page fractions."""
    data = pytesseract.image_to_data(image, config=f"--psm {ALIGN_OCR_PSM}", output_type=Output.DICT)
    words = []
    for i, raw in enumerate(data["text"]):
        text = normalize_word(raw)
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            continue
        if len(text) < ALIGN_MIN_WORD_LEN or conf < ALIGN_MIN_WORD_CONF:
            continue
        words.append(
            PageWord(
                text=text,
                x=data["left"][i] / image.width,
                y=(data["top"][i] + data["height"][i] / 2) / image.height,
            )
        )
    return words


# eq=False: the matched-landmark rows are a numpy array, which makes a generated __eq__ return an array
# and a generated __hash__ raise; an Alignment is compared by identity, like the page it describes.
@dataclass(frozen=True, eq=False)
class Alignment:
    """Template frame -> this page. page x = sx * x + kx * y + tx, page y = sy * y + ky * x + ty, plus a local nudge.
    kx and ky are the cross terms a small page tilt leaves (ky ~ tilt, kx ~ -tilt, as page fractions); 0 when the fit has none."""

    sx: float
    tx: float
    sy: float
    ty: float
    landmarks: int  # distinct template landmarks that agreed
    rms: float  # leftover of those after the global fit, as a fraction of the page
    matched: tuple[int, ...]  # indices into the template's landmark list
    _rows: np.ndarray  # one row per matched landmark: template x, y, page x, y
    kx: float = 0.0
    ky: float = 0.0

    def _global(self, x, y):
        """Where the global fit puts template point(s) (x, y) on the page."""
        return self.sx * x + self.kx * y + self.tx, self.sy * y + self.ky * x + self.ty

    def map_box(self, box: BBox) -> BBox:
        """A template box moved to where it sits on this page (clamped to the page)."""
        x0, y0, x1, y1 = box
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        rows = self._rows
        dist = np.hypot(rows[:, 0] - cx, rows[:, 1] - cy)
        order = np.argsort(dist)[:ALIGN_LOCAL_NEIGHBOURS]
        near = order[dist[order] <= ALIGN_LOCAL_RADIUS]
        dx = dy = 0.0
        if len(near) >= ALIGN_LOCAL_MIN_NEIGHBOURS:
            # Only landmarks close to THIS box say anything about its leftover, and a median of at least
            # three outvotes one mismatched pair (a typed "bangkok" paired with an address word). A
            # distance-weighted version with a two-landmark minimum let the nearest bad pair win and refused
            # 4 of 10 labelled pages; this one refuses 1 (a different print) - see ALIGN_LOCAL_MIN_NEIGHBOURS.
            gx, gy = self._global(rows[near, 0], rows[near, 1])
            dx = float(np.median(rows[near, 2] - gx))
            dy = float(np.median(rows[near, 3] - gy))

        def clamp(v: float) -> float:
            return min(1.0, max(0.0, v))

        def at_least_a_sliver(lo: float, hi: float) -> tuple[float, float]:
            # A box pushed past the page edge clamps to nothing. Keep a thin strip ON the edge rather than
            # handing back the stored box: the strip reads blank and the field is flagged, where the stored
            # box would read some other part of the page as if it were this field.
            if hi - lo >= _MIN_EXTENT:
                return lo, hi
            lo = min(lo, 1.0 - _MIN_EXTENT)
            return lo, lo + _MIN_EXTENT

        # the cross terms move a box as a whole (by where its centre sits), they do not skew its edges
        cross_x, cross_y = self.kx * cy, self.ky * cx
        nx0, nx1 = at_least_a_sliver(clamp(self.sx * x0 + cross_x + self.tx + dx), clamp(self.sx * x1 + cross_x + self.tx + dx))
        ny0, ny1 = at_least_a_sliver(clamp(self.sy * y0 + cross_y + self.ty + dy), clamp(self.sy * y1 + cross_y + self.ty + dy))
        return (nx0, ny0, nx1, ny1)

    def summary(self) -> dict:
        return {
            "landmarks": self.landmarks,
            "scale_x": round(self.sx, 4),
            "scale_y": round(self.sy, 4),
            "shift_x": round(self.tx, 4),
            "shift_y": round(self.ty, 4),
            "cross_x": round(self.kx, 4),
            "cross_y": round(self.ky, 4),
            "rms": round(self.rms, 5),
        }


def _same_word(a: str, b: str) -> bool:
    return a == b or (abs(len(a) - len(b)) <= 2 and fuzz.ratio(a, b) >= ALIGN_MATCH_RATIO)


def _solve_two(a: tuple, b: tuple) -> Optional[tuple[float, float, float, float]]:
    """Two correspondences (template x, y, page x, y) -> (sx, tx, sy, ty), None if they cannot pin both axes."""
    if abs(a[0] - b[0]) < _MIN_SPREAD or abs(a[1] - b[1]) < _MIN_SPREAD:
        return None
    sx = (a[2] - b[2]) / (a[0] - b[0])
    sy = (a[3] - b[3]) / (a[1] - b[1])
    return sx, a[2] - sx * a[0], sy, a[3] - sy * a[1]


def fit_alignment(
    landmarks: Sequence[TemplateLandmark],
    words: Sequence[PageWord],
    *,
    min_landmarks: int = ALIGN_MIN_LANDMARKS,
    min_fraction: float = ALIGN_MIN_LANDMARK_FRACTION,
) -> Optional[Alignment]:
    """
    Fits the template's landmarks to the page's words. None when fewer than ALIGN_MIN_LANDMARKS
    (and ALIGN_MIN_LANDMARK_FRACTION of the template's own) agree: the page does not carry this
    layout's printed words in a consistent arrangement, and a guess would move boxes onto the
    wrong text. The minimums are keyword-only so scripts/build_landmarks.py can relax them while
    it is still deciding which words ARE landmarks; the pipeline never overrides them.
    """
    needed = max(min_landmarks, int(min_fraction * len(landmarks)))
    if len(landmarks) < needed or len(words) < needed:
        return None

    pairs = [
        (i, lm.x, lm.y, w.x, w.y)
        for i, lm in enumerate(landmarks)
        for w in words
        if _same_word(lm.text, w.text)
    ]
    if len({p[0] for p in pairs}) < needed:
        return None
    ids = np.array([p[0] for p in pairs])
    P = np.array([[p[1], p[2], p[3], p[4]] for p in pairs])

    rnd = random.Random(_RANSAC_SEED)
    lo, hi = ALIGN_SCALE_RANGE
    best: Optional[tuple[int, np.ndarray]] = None
    for _ in range(ALIGN_RANSAC_ITERS):
        a, b = rnd.sample(range(len(pairs)), 2)
        model = _solve_two(tuple(P[a]), tuple(P[b]))
        if model is None or not (lo <= model[0] <= hi and lo <= model[2] <= hi):
            continue
        sx, tx, sy, ty = model
        ok = (np.abs(sx * P[:, 0] + tx - P[:, 2]) < ALIGN_RANSAC_TOL) & (np.abs(sy * P[:, 1] + ty - P[:, 3]) < ALIGN_RANSAC_TOL)
        score = len(set(ids[ok]))
        if best is None or score > best[0]:
            best = (score, ok)
    if best is None or best[0] < needed:
        return None

    ok = best[1]
    for _ in range(2):  # least squares on the inliers, then collect the inliers again
        sx, tx = np.linalg.lstsq(np.c_[P[ok, 0], np.ones(ok.sum())], P[ok, 2], rcond=None)[0]
        sy, ty = np.linalg.lstsq(np.c_[P[ok, 1], np.ones(ok.sum())], P[ok, 3], rcond=None)[0]
        ok = (np.abs(sx * P[:, 0] + tx - P[:, 2]) < ALIGN_RANSAC_TOL) & (np.abs(sy * P[:, 1] + ty - P[:, 3]) < ALIGN_RANSAC_TOL)
    if not (lo <= sx <= hi and lo <= sy <= hi):
        return None

    # The cross terms: a page that is still tilted after deskew puts every row a little lower on one side than the other, which a
    # scale and a shift per axis cannot follow. Refit the inliers with them, keeping the result only when it stays a small tilt (a
    # large cross term is a different layout bending the fit, not a tilt) and does not lose landmarks.
    kx = ky = 0.0
    if ALIGN_MAX_CROSS > 0:
        ok2 = ok
        for _ in range(2):
            A = np.c_[P[ok2, 0], P[ok2, 1], np.ones(ok2.sum())]
            fx, fy = np.linalg.lstsq(A, P[ok2, 2], rcond=None)[0], np.linalg.lstsq(A, P[ok2, 3], rcond=None)[0]
            ok2 = (np.abs(fx[0] * P[:, 0] + fx[1] * P[:, 1] + fx[2] - P[:, 2]) < ALIGN_RANSAC_TOL) & (np.abs(fy[1] * P[:, 1] + fy[0] * P[:, 0] + fy[2] - P[:, 3]) < ALIGN_RANSAC_TOL)
        if (abs(fx[1]) <= ALIGN_MAX_CROSS and abs(fy[0]) <= ALIGN_MAX_CROSS and lo <= fx[0] <= hi and lo <= fy[1] <= hi
                and len(set(ids[ok2])) >= max(needed, len(set(ids[ok])))):
            sx, kx, tx, ky, sy, ty = float(fx[0]), float(fx[1]), float(fx[2]), float(fy[0]), float(fy[1]), float(fy[2])
            ok = ok2

    def where(k):
        return sx * P[k, 0] + kx * P[k, 1] + tx, sy * P[k, 1] + ky * P[k, 0] + ty

    # One correspondence per landmark: the page word closest to where the model puts it.
    chosen: dict[int, tuple[float, int]] = {}
    for k in np.where(ok)[0]:
        px_, py_ = where(k)
        err = abs(px_ - P[k, 2]) + abs(py_ - P[k, 3])
        if ids[k] not in chosen or err < chosen[ids[k]][0]:
            chosen[ids[k]] = (err, int(k))
    if len(chosen) < needed:
        return None
    order = sorted(chosen)
    rows = P[[chosen[i][1] for i in order]]
    rms = float(np.sqrt(np.mean((sx * rows[:, 0] + kx * rows[:, 1] + tx - rows[:, 2]) ** 2 + (sy * rows[:, 1] + ky * rows[:, 0] + ty - rows[:, 3]) ** 2)))
    return Alignment(
        sx=float(sx), tx=float(tx), sy=float(sy), ty=float(ty),
        landmarks=len(order), rms=rms, matched=tuple(order), _rows=rows, kx=float(kx), ky=float(ky),
    )


def align_page(image: Image.Image, landmarks: Sequence[TemplateLandmark]) -> Optional[Alignment]:
    """OCR the page and fit. Convenience for callers that have not already read the page's words."""
    if not landmarks:
        return None
    return fit_alignment(landmarks, read_page_words(image))


def landmarks_from_words(words: Sequence[PageWord]) -> list[TemplateLandmark]:
    """A page's usable words as candidate landmarks (the builder script then keeps the ones that recur)."""
    return [TemplateLandmark(text=w.text, x=min(1.0, max(0.0, w.x)), y=min(1.0, max(0.0, w.y))) for w in words]
