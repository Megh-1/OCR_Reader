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

from app.modules.template_store import get_latest_template, list_known_doc_types
from app.schemas.template import TemplateAnchor, TemplateOut
from app.utils.geometry import relative_bbox_to_pixels


@dataclass
class IdentificationResult:
    confirmed: bool
    template_id: Optional[str]
    version: Optional[int]
    reason: Optional[str]  # "no_match" | "ambiguous_match" | "unknown_type_hint" | None (when confirmed)
    per_anchor_scores: dict[str, dict[str, float]] = field(default_factory=dict)  # {template_id: {anchor_text: score}}


def _score_anchor(image: Image.Image, anchor: TemplateAnchor) -> float:
    """OCRs the anchor's region and fuzzy-scores it against the expected text. 0-100 scale."""
    left, top, right, bottom = relative_bbox_to_pixels(anchor.bbox, image.width, image.height)
    crop = image.crop((left, top, right, bottom))
    text = pytesseract.image_to_string(crop).strip()
    return fuzz.ratio(text, anchor.text)


def _check_template(image: Image.Image, template: TemplateOut) -> tuple[bool, dict[str, float]]:
    """Runs every anchor of `template` against `image`. Passes only if ALL do (Assumption 1)."""
    scores: dict[str, float] = {}
    all_passed = True
    for anchor in template.anchors:
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

    if type_hint is not None:
        template = get_latest_template(type_hint)
        if template is None:
            # A hint that doesn't resolve is informative on its own - never
            # silently fall back to scanning every known template instead.
            return IdentificationResult(
                confirmed=False, template_id=None, version=None, reason="unknown_type_hint"
            )

        passed, scores = _check_template(image, template)
        per_anchor_scores = {template.template_id: scores}
        if passed:
            return IdentificationResult(
                confirmed=True,
                template_id=template.template_id,
                version=template.version,
                reason=None,
                per_anchor_scores=per_anchor_scores,
            )
        # The hint doesn't bypass verification - it only narrows which
        # template gets checked.
        return IdentificationResult(
            confirmed=False, template_id=None, version=None, reason="no_match", per_anchor_scores=per_anchor_scores
        )

    per_anchor_scores = {}
    matches: list[TemplateOut] = []
    for doc_type in list_known_doc_types():
        template = get_latest_template(doc_type)
        if template is None:
            continue  # doc_type came from the store itself; a race here is not this function's problem to solve
        passed, scores = _check_template(image, template)
        per_anchor_scores[doc_type] = scores
        if passed:
            matches.append(template)

    if len(matches) == 1:
        template = matches[0]
        return IdentificationResult(
            confirmed=True,
            template_id=template.template_id,
            version=template.version,
            reason=None,
            per_anchor_scores=per_anchor_scores,
        )
    if len(matches) == 0:
        return IdentificationResult(
            confirmed=False, template_id=None, version=None, reason="no_match", per_anchor_scores=per_anchor_scores
        )
    # More than one template's anchors all passed - never guess among them.
    return IdentificationResult(
        confirmed=False, template_id=None, version=None, reason="ambiguous_match", per_anchor_scores=per_anchor_scores
    )
