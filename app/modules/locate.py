"""
Where on the page a read value sits, for the Review screen's highlight.

Display only: it never feeds a tier, a grounding decision or a score (grounding.py decides what is on the page). The stored page image is read
once with Tesseract (word boxes, whole page), the words are kept in a small cache, and the run of words that best matches the value's source
text is returned as one box per printed line, in fractions of the page (the same space the field boxes of a template use).
"""

from __future__ import annotations

import hashlib
import io
from functools import lru_cache
from typing import Optional

import pytesseract
from PIL import Image
from rapidfuzz import fuzz

MIN_SCORE = 85.0  # a run of words this similar to the source text is shown; below it the page has nothing worth pointing at
MIN_SCORE_SHORT = 90.0  # ... for a source text of 4 characters or fewer ("BEY" is not "by"): short strings match anything loosely
CLUSTER_TOKEN_SCORE = 85.0  # one word of the value against one OCR word
CLUSTER_COVERAGE = 0.6  # the share of the value's words the group must cover
CLUSTER_DX, CLUSTER_DY = 0.45, 0.12  # how far apart (fractions of the page) words of one value may sit
Word = tuple[str, tuple[float, float, float, float]]


@lru_cache(maxsize=64)
def _words_cached(digest: str, image_bytes: bytes) -> tuple[Word, ...]:  # noqa: ARG001 - the digest is the cache key, the bytes are the input
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    width, height = image.size
    data = pytesseract.image_to_data(image, config="--psm 6", output_type=pytesseract.Output.DICT)
    words: list[Word] = []
    for text, conf, x, y, w, h in zip(data["text"], data["conf"], data["left"], data["top"], data["width"], data["height"]):
        if not text.strip() or float(conf) < 0:
            continue
        words.append((text.strip(), (x / width, y / height, (x + w) / width, (y + h) / height)))
    return tuple(words)


def page_words(image_bytes: bytes) -> tuple[Word, ...]:
    return _words_cached(hashlib.sha1(image_bytes).hexdigest(), image_bytes)


def _lines(boxes: list[tuple[float, float, float, float]]) -> list[list[float]]:
    """One box per printed line: consecutive word boxes whose vertical centres sit within one word height of each other are merged."""
    merged: list[list[float]] = []
    for x0, y0, x1, y1 in boxes:
        if merged:
            last = merged[-1]
            if abs((y0 + y1) / 2 - (last[1] + last[3]) / 2) <= max(y1 - y0, last[3] - last[1]):
                last[0], last[1], last[2], last[3] = min(last[0], x0), min(last[1], y0), max(last[2], x1), max(last[3], y1)
                continue
        merged.append([x0, y0, x1, y1])
    return merged


def locate(words: tuple[Word, ...] | list[Word], source_text: str) -> list[list[float]]:
    """The boxes (one per line) of the run of page words that best matches `source_text`, or [] when nothing matches well enough."""
    needle = " ".join(source_text.lower().split())
    if not needle or not words:
        return []
    count = len(needle.split())
    lowered = [w[0].lower() for w in words]
    best: Optional[tuple[float, int, int]] = None
    for size in sorted({max(1, count - 1), count, count + 1}):
        for start in range(0, len(words) - size + 1):
            score = fuzz.ratio(" ".join(lowered[start:start + size]), needle)
            if best is None or score > best[0]:
                best = (score, start, size)
    if best is not None and best[0] >= (MIN_SCORE_SHORT if len(needle) <= 4 else MIN_SCORE):
        _, start, size = best
        return _lines([w[1] for w in words[start:start + size]])
    return _cluster(words, lowered, needle.split())


def _cluster(words, lowered: list[str], tokens: list[str]) -> list[list[float]]:
    """A form's columns break the page's reading order, so a multi-line value is not one run of OCR words. Fall back to the words that match
    the value's own words one by one (>= 85 each) and keep the group that sits together on the page and covers most of them."""
    wanted = [t for t in dict.fromkeys(tokens) if len(t) >= 3]
    if len(wanted) < 2:
        return []
    hits = [(t, i) for t in wanted for i, w in enumerate(lowered) if fuzz.ratio(t, w) >= CLUSTER_TOKEN_SCORE]
    best: list[int] = []
    for _, seed in hits:
        sx, sy = words[seed][1][0], words[seed][1][1]
        chosen: dict[str, int] = {}
        for t, i in hits:
            x, y = words[i][1][0], words[i][1][1]
            if abs(x - sx) <= CLUSTER_DX and abs(y - sy) <= CLUSTER_DY:
                chosen.setdefault(t, i)
        if len(chosen) > len(best):
            best = sorted(chosen.values())
    if len(best) < max(2, int(len(wanted) * CLUSTER_COVERAGE + 0.999)):
        return []
    return _lines(sorted((words[i][1] for i in best), key=lambda b: (round(b[1], 2), b[0])))
