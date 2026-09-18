"""
Module 6 - Field Extraction Engine.

Runs only after Module 5 has returned confirmed=True for a specific
(template_id, version) pair. Fetches that EXACT version via Module 4's
get_template_version - never "latest" - since the version Module 5 actually
verified anchors against is the only one whose field coordinates are known
to correspond to this image.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pytesseract
from PIL import Image
from pytesseract import Output

from app.config import (
    DEFAULT_PSM,
    FIELD_CROP_MARGIN_PX,
    PSM_BY_DATA_TYPE,
    PSM_CANDIDATES,
    RULE_ARTIFACT_CHARS,
)
from app.modules.template_store import get_template_version
from app.schemas.template import TemplateField, TemplateOut
from app.utils.geometry import clamped_crop_box


@dataclass
class FieldExtractionResult:
    field_id: str
    raw_text: str
    word_confidences: list[float]
    aggregate_confidence: float
    psm_used: int


def _words_and_confidences(data: dict) -> tuple[list[str], list[float]]:
    """
    Pulls actual words and their confidences out of pytesseract's
    image_to_data DICT output. Tesseract marks structural rows it has no
    confidence for (page/block/paragraph/line, not real words) with an
    empty text and conf=-1; those must never be averaged in as if they were
    real low-confidence reads.
    """
    words = [text for text in data["text"] if text.strip()]
    confidences = [float(c) for c in data["conf"] if float(c) != -1]
    return words, confidences


def _aggregate_confidence(confidences: list[float]) -> float:
    return sum(confidences) / len(confidences) if confidences else 0.0


def _trim_rule_artifacts(text: str) -> str:
    """
    Strips leftover form-rule marks from the ENDS of a read value.

    A field on a ruled form sits inside a printed cell, so a crop of it tends
    to catch a fragment of the surrounding rule, which Tesseract renders as a
    stray "|", "_", "~" or bracket - "170" comes back as "| 170", "500.00" as
    "~ 500.00". Module 2 erases full-length rules before OCR; this removes the
    short fragments that survive at a crop boundary, where a rule is too short
    to be recognizable as one.

    Deliberately edge-only, and deliberately limited to characters that never
    legitimately begin or end a field value. It cannot repair a misread INSIDE
    a value, which is the whole point: undoing an artifact our own cropping
    introduced is not the same as guessing at content, and a genuine OCR error
    still reaches Module 7 intact and still gets flagged.
    """
    return text.strip(RULE_ARTIFACT_CHARS + " ")


def _read_at_psm(crop: Image.Image, psm: int) -> tuple[str, list[float], float]:
    data = pytesseract.image_to_data(crop, output_type=Output.DICT, config=f"--psm {psm}")
    words, confidences = _words_and_confidences(data)
    return _trim_rule_artifacts(" ".join(words)), confidences, _aggregate_confidence(confidences)


def extract_field(image: Image.Image, field: TemplateField) -> FieldExtractionResult:
    """
    Reads one field, trying several page-segmentation modes and keeping
    whichever Tesseract was most confident about.

    A single fixed PSM per data type - the reference doc's suggestion, and
    what this did originally - assumes one segmentation suits every field of a
    given type. Measured on a real air waybill it does not, and the mode that
    wins varies field by field with no property of the field predicting it:
    the consignee reads exactly at PSM 6 but becomes "VTS CLIMA LLG" at 7,
    while the flight number reads exactly at 4 and becomes "EKO565/27FEB" at
    6. Since Tesseract reports its own confidence, the mode can be chosen per
    field from evidence instead of guessed in advance from the schema.

    Empty reads are discarded before comparing, because an empty result can
    carry a high confidence drawn from non-text layout elements - PSM 4
    returned nothing at confidence 95 on a field two other modes read
    perfectly.

    The template's declared PSM is tried first and wins ties, so a field whose
    declared mode is already right behaves exactly as before.
    """
    box = clamped_crop_box(field.bbox, image.width, image.height, FIELD_CROP_MARGIN_PX)
    crop = image.crop(box)

    declared_psm = PSM_BY_DATA_TYPE.get(field.data_type, DEFAULT_PSM)
    candidate_modes = [declared_psm] + [psm for psm in PSM_CANDIDATES if psm != declared_psm]

    best: tuple[str, list[float], float, int] | None = None
    for psm in candidate_modes:
        text, confidences, aggregate = _read_at_psm(crop, psm)
        if not text:
            continue
        if best is None or aggregate > best[2]:
            best = (text, confidences, aggregate, psm)

    if best is None:  # every mode came back empty - a genuinely blank region
        return FieldExtractionResult(
            field_id=field.id, raw_text="", word_confidences=[], aggregate_confidence=0.0, psm_used=declared_psm
        )

    text, confidences, aggregate, psm = best
    return FieldExtractionResult(
        field_id=field.id,
        raw_text=text,
        word_confidences=confidences,
        aggregate_confidence=aggregate,
        psm_used=psm,
    )


def extract_fields(template_id: str, version: int, image_bytes: bytes) -> dict[str, FieldExtractionResult]:
    """
    Entry point. `image_bytes` is Module 2's output shape. Fetches the
    exact confirmed version, never "latest" for this doc_type - Module 5
    may have confirmed against an older version than whatever is newest by
    the time this runs.
    """
    template: TemplateOut | None = get_template_version(template_id, version)
    if template is None:
        raise ValueError(f"No template found for {template_id!r} version {version} - was it deleted after Module 5 ran?")

    image = Image.open(io.BytesIO(image_bytes))
    return {field.id: extract_field(image, field) for field in template.fields}
