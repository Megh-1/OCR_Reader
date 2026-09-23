from dataclasses import dataclass


@dataclass(frozen=True)
class TextWord:
    """One word of a PDF's embedded text layer. `bbox` is (x0, y0, x1, y1) as
    fractions of the page, 0.0-1.0, in the DISPLAYED orientation - the same
    space a template's field bboxes are stored in."""

    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class TextLayer:
    """The words a PDF page carries natively, positioned in fractional page
    coordinates. Only ever built for pages with real (visible) embedded text -
    see app/modules/ingestion.py."""

    words: tuple[TextWord, ...]
