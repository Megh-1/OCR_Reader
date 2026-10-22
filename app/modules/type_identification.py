"""
Module 5 - Document Type Identification (anchor verification).

The highest-stakes decision in the pipeline: confirm which template
applies before anything is extracted, so the system never guesses. Two
design choices the reference doc leaves implicit are made explicit here:

  - A template match requires ALL of its anchors to individually pass
    their match_threshold (AND, not "any one is enough") - the more
    conservative reading, matching the doc's "err toward flagging over
    guessing" principle.
  - With no type_hint, if MORE THAN ONE template's anchors all pass,
    that is an ambiguous_match rejection, never an arbitrary pick among
    the candidates.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional

import pytesseract
from PIL import Image
from rapidfuzz import fuzz

from app.config import ANCHOR_CROP_MARGIN_PX
from app.modules.alignment import Alignment, PageWord, fit_alignment, read_page_words
from app.modules.template_store import get_latest_template, list_known_doc_types
from app.schemas.template import TemplateAnchor, TemplateOut
from app.utils.geometry import clamped_crop_box


@dataclass
class IdentificationResult:
    confirmed: bool
    template_id: Optional[str]
    version: Optional[int]
    reason: Optional[str]  # "no_match" | "ambiguous_match" | "unknown_type_hint" | None (when confirmed)
    per_anchor_scores: dict[str, dict[str, float]] = field(default_factory=dict)  # {template_id: {anchor_text: score}}
    # Set when the confirmed template's boxes were moved to fit this page (Module 6 must use the
    # same ones that verified it). None for a template without landmarks, or too few agreed.
    alignment: Optional[Alignment] = None


def _score_anchor(image: Image.Image, anchor: TemplateAnchor) -> float:
    """
    OCRs the anchor's region and fuzzy-scores it against the expected text.
    0-100 scale. Crops with a margin (config.ANCHOR_CROP_MARGIN_PX), not a
    bare bbox - see that constant's own comment for why a zero-slack crop
    turned out to be fragile against real-world tilt/rounding.

    Case and line breaks do not count: real forms print "Not negotiable" where the template
    reads "Not Negotiable", and a label wrapped over two lines reads with a newline where the
    template has a space. Measured on 16 real waybills this alone lifted the first anchor from
    59-69 to 70+ on pages whose boxes were already in the right place.
    """
    box = clamped_crop_box(anchor.bbox, image.width, image.height, ANCHOR_CROP_MARGIN_PX)
    crop = image.crop(box)
    text = " ".join(pytesseract.image_to_string(crop).split())
    return fuzz.ratio(text.lower(), " ".join(anchor.text.split()).lower())


def _check_template(
    image: Image.Image, template: TemplateOut, alignment: Optional[Alignment] = None
) -> tuple[bool, dict[str, float]]:
    """Runs every anchor of `template` against `image`. Passes only if ALL do (Assumption 1).

    With an `alignment`, each anchor is read where it sits on THIS page rather than where the
    template stored it."""
    scores: dict[str, float] = {}
    all_passed = True
    for anchor in template.anchors:
        if alignment is not None:
            anchor = anchor.model_copy(update={"bbox": alignment.map_box(anchor.bbox)})
        score = _score_anchor(image, anchor)
        scores[anchor.text] = score
        # anchor.match_threshold is 0.0-1.0; fuzz.ratio is 0-100 - the *100 here is deliberate.
        if score < anchor.match_threshold * 100:
            all_passed = False
    return all_passed, scores


def identify_document_type(image_bytes: bytes, type_hint: Optional[str] = None) -> IdentificationResult:
    """
    Entry point. `image_bytes` is Module 2's output shape (a preprocessed
    page image). Never raises on a failed match - a rejection is a normal,
    expected result (reason="no_match" / "ambiguous_match" /
    "unknown_type_hint"), not an exception.
    """
    image = Image.open(io.BytesIO(image_bytes))

    # Reading the whole page for alignment costs ~1.5 s, so it happens at most once per page and
    # only if some template to be checked has landmarks.
    page_words: Optional[list[PageWord]] = None

    def align(template: TemplateOut) -> Optional[Alignment]:
        nonlocal page_words
        if not template.landmarks:
            return None
        if page_words is None:
            page_words = read_page_words(image)
        return fit_alignment(template.landmarks, page_words)

    def check(template: TemplateOut) -> tuple[bool, dict[str, float], Optional[Alignment]]:
        alignment = align(template)
        # Aligned boxes verify the page whenever an alignment exists, and Module 6 then reads with
        # the same ones - never "passed at the stored position, read at the moved one".
        passed, scores = _check_template(image, template, alignment) if alignment else _check_template(image, template)
        return passed, scores, alignment

    if type_hint is not None:
        template = get_latest_template(type_hint)
        if template is None:
            # A hint that doesn't resolve is informative on its own - never
            # silently fall back to scanning every known template instead.
            return IdentificationResult(
                confirmed=False, template_id=None, version=None, reason="unknown_type_hint"
            )

        passed, scores, alignment = check(template)
        per_anchor_scores = {template.template_id: scores}
        if passed:
            return IdentificationResult(
                confirmed=True,
                template_id=template.template_id,
                version=template.version,
                reason=None,
                per_anchor_scores=per_anchor_scores,
                alignment=alignment,
            )
        # The hint doesn't bypass verification - it only narrows which
        # template gets checked.
        return IdentificationResult(
            confirmed=False, template_id=None, version=None, reason="no_match", per_anchor_scores=per_anchor_scores
        )

    per_anchor_scores = {}
    matches: list[tuple[TemplateOut, Optional[Alignment]]] = []
    for doc_type in list_known_doc_types():
        template = get_latest_template(doc_type)
        if template is None:
            continue  # doc_type came from the store itself; a race here is not this function's problem to solve
        if template.hints.hint_only:
            continue  # only ever chosen by an explicit type_hint, never by scanning
        passed, scores, alignment = check(template)
        per_anchor_scores[doc_type] = scores
        if passed:
            matches.append((template, alignment))

    if len(matches) == 1:
        template, alignment = matches[0]
        return IdentificationResult(
            confirmed=True,
            template_id=template.template_id,
            version=template.version,
            reason=None,
            per_anchor_scores=per_anchor_scores,
            alignment=alignment,
        )
    if len(matches) == 0:
        return IdentificationResult(
            confirmed=False, template_id=None, version=None, reason="no_match", per_anchor_scores=per_anchor_scores
        )
    # More than one template's anchors all passed - never guess among them.
    return IdentificationResult(
        confirmed=False, template_id=None, version=None, reason="ambiguous_match", per_anchor_scores=per_anchor_scores
    )
