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

from app.config import DEFAULT_PSM, FIELD_CROP_MARGIN_PX, PSM_BY_DATA_TYPE
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


def extract_field(image: Image.Image, field: TemplateField) -> FieldExtractionResult:
    box = clamped_crop_box(field.bbox, image.width, image.height, FIELD_CROP_MARGIN_PX)
    crop = image.crop(box)

    psm = PSM_BY_DATA_TYPE.get(field.data_type, DEFAULT_PSM)
    data = pytesseract.image_to_data(crop, output_type=Output.DICT, config=f"--psm {psm}")

    words, confidences = _words_and_confidences(data)

    return FieldExtractionResult(
        field_id=field.id,
        raw_text=" ".join(words),
        word_confidences=confidences,
        aggregate_confidence=_aggregate_confidence(confidences),
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
