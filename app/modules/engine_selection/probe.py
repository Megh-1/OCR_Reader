"""
The document probe: a few cheap facts about one page that the selector may use to choose candidate engines.

What is measured, and what is NOT, stated plainly because a probe that pretends is worse than none:

  has_text_layer   exact (the PDF carries real, visible text).
  sharpness        variance of the Laplacian of the page at 1200 px wide: high = crisp edges, low = blur. `quality` bins it with
                   config.ENGINE_SHARP_MIN / ENGINE_SHARP_POOR, which were set on synthetic pages and are PROVISIONAL.
  contrast         standard deviation of the grey levels (a faded scan or a dim photo is low).
  script           the dominant Unicode script of the text layer's words (latin, cyrillic, arabic, cjk, ...). Only for a page WITH a
                   text layer: there are no characters to look at on a raster without running OCR, which is the work being chosen.
                   It is a script, not a language (English and French are both `latin`).
  handwriting_share  NOT MEASURED. There is no handwriting detector in the app; the field exists so a selector rule can use one the day
                   there is, and it is None until then. No rule in selector.py reads it.
"""

from __future__ import annotations

import io
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Optional

import cv2
import numpy as np
from PIL import Image

from app import config
from app.models.text_layer import TextLayer

PROBE_WIDTH_PX = 1200


@dataclass(frozen=True)
class DocFeatures:
    has_text_layer: bool
    sharpness: Optional[float] = None
    contrast: Optional[float] = None
    quality: str = "unknown"  # good | soft | poor | unknown
    script: Optional[str] = None
    handwriting_share: Optional[float] = None  # not measured (see the module docstring)

    def as_dict(self) -> dict:
        return asdict(self)


def quality_of(sharpness: Optional[float]) -> str:
    if sharpness is None:
        return "unknown"
    if sharpness < config.ENGINE_SHARP_POOR:
        return "poor"
    return "soft" if sharpness < config.ENGINE_SHARP_MIN else "good"


def _script(char: str) -> Optional[str]:
    if not char.isalpha():
        return None
    name = unicodedata.name(char, "")
    for key, label in (("LATIN", "latin"), ("CYRILLIC", "cyrillic"), ("ARABIC", "arabic"), ("HEBREW", "hebrew"), ("GREEK", "greek"),
                       ("DEVANAGARI", "devanagari"), ("THAI", "thai"), ("HANGUL", "korean"), ("HIRAGANA", "japanese"),
                       ("KATAKANA", "japanese"), ("CJK", "cjk")):
        if name.startswith(key):
            return label
    return "other"


def script_of(layer: Optional[TextLayer]) -> Optional[str]:
    """The script most of a text layer's letters are in; None when it has no letters."""
    if layer is None:
        return None
    counts = Counter(s for w in layer.words for ch in w.text if (s := _script(ch)))
    return counts.most_common(1)[0][0] if counts else None


def image_stats(page_bytes: bytes) -> tuple[Optional[float], Optional[float]]:
    """(sharpness, contrast) of a page image, or (None, None) when it cannot be decoded."""
    try:
        with Image.open(io.BytesIO(page_bytes)) as im:
            grey = np.asarray(im.convert("L"))
    except Exception:  # an undecodable page is a probe with fewer facts, not a failure of the document
        return None, None
    if grey.size == 0:
        return None, None
    h, w = grey.shape
    if w != PROBE_WIDTH_PX:  # sharpness depends on scale, so every page is looked at at one width
        grey = cv2.resize(grey, (PROBE_WIDTH_PX, max(1, round(h * PROBE_WIDTH_PX / w))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(grey, cv2.CV_64F).var()), float(grey.std())


def probe_page(page_bytes: bytes, text_layer: Optional[TextLayer] = None) -> DocFeatures:
    sharpness, contrast = image_stats(page_bytes)
    return DocFeatures(has_text_layer=text_layer is not None, sharpness=sharpness, contrast=contrast, quality=quality_of(sharpness),
                       script=script_of(text_layer))
