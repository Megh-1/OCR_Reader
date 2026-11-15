"""Grounding: is a read value actually on the page?

A value is grounded when it occurs in the page's text layer (a digital PDF) or, for a scan or photo, matches the page's own words as a whole-page
Tesseract read finds them (fuzzy). It looks only at the page, never at another reading, so a model that invents a plausible value is caught.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from typing import Optional

import pytesseract
from PIL import Image
from rapidfuzz import fuzz

from app.models.text_layer import TextLayer

GROUNDED_FUZZY = 90.0  # a scan / photo value must match the page's OCR words at least this well (partial ratio)
HALLUCINATION_FUZZY = 80.0  # the scorer counts a value as invented when it matches nothing on the page above this


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


@dataclass(frozen=True)
class PageText:
    layer: str  # words of the real text layer ("" when the page has none)
    ocr: str  # whole-page Tesseract words ("" when not computed)

    @property
    def has_layer(self) -> bool:
        return bool(self.layer)

    def best_score(self, value: str) -> float:
        needle = _squash(value)
        if not needle:
            return 0.0
        scores = [0.0]
        for hay in (self.layer, self.ocr):
            if not hay:
                continue
            scores.append(100.0 if needle in hay else fuzz.partial_ratio(needle, hay))
        return max(scores)

    def grounded(self, value: str) -> bool:
        """The rule of the tiers: digital pages need the value in the text layer as whole words, scans and photos fuzzy >= GROUNDED_FUZZY against the OCR words."""
        needle = _squash(value)
        if not needle:
            return False
        if self.has_layer:
            # Whole printed words, punctuation included: a reader that drops a trailing comma or the quotes round a phrase has not copied
            # what is on the page, and a plain substring test would still find it there.
            return re.search(r"(?<!\S)" + re.escape(needle) + r"(?!\S)", self.layer) is not None
        return self.best_score(value) >= GROUNDED_FUZZY


def page_text(image_bytes: Optional[bytes], layer: Optional[TextLayer], ocr: bool = True) -> PageText:
    layer_text = _squash(" ".join(w.text for w in layer.words)) if layer is not None else ""
    ocr_text = ""
    if ocr and image_bytes:
        ocr_text = _squash(pytesseract.image_to_string(Image.open(io.BytesIO(image_bytes)), config="--psm 6"))
    return PageText(layer=layer_text, ocr=ocr_text)
