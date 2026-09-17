"""
Module 7 - Validation Layer.

The last checkpoint before Module 8 compiles output as trusted. A quiet
bug in Module 5 or 6 still gets caught somewhere downstream of it; a quiet
bug here does not - this module IS the downstream. A field is accepted
only if BOTH its format/regex check and its confidence check pass, and a
failing field's reasons list collects every check that failed, never just
the first one hit.

Assumptions made explicit here (no allowed_values field exists in the
schema, and the reference doc's own blood-group example already expresses
its enum constraint as a regex):
  - "enum" fields are validated via validation_regex, the same mechanism
    as "string" fields - there is no separate set-membership check.
  - An "enum" field with validation_regex == null is a template-authoring
    error, not something to silently accept: it is automatically flagged
    with reason "enum_missing_regex".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Optional

from pydantic import BaseModel, ValidationError, field_validator

from app.modules.field_extraction import FieldExtractionResult
from app.schemas.template import TemplateField, TemplateOut


@dataclass
class FieldValidationResult:
    field_id: str
    value: str  # the ORIGINAL raw_text, unstripped - stripping is only for the comparison
    confidence: float
    accepted: bool
    reasons: list[str] = dataclass_field(default_factory=list)  # empty when accepted


@dataclass
class ValidationSummary:
    accepted: dict[str, FieldValidationResult]
    flagged: dict[str, FieldValidationResult]


class _FormatCheck(BaseModel):
    """
    The declarative regex enforcement itself - reusing Pydantic v2 the same
    way the API request schemas do, rather than a bare re.fullmatch call
    sitting in validate_field's body. `pattern` is declared before `value`
    so it's already in `info.data` by the time `value` is validated.
    """

    pattern: Optional[str] = None
    value: str

    @field_validator("value")
    @classmethod
    def _matches_pattern(cls, v: str, info) -> str:
        pattern = info.data.get("pattern")
        if pattern is not None and re.fullmatch(pattern, v) is None:
            raise ValueError(f"{v!r} does not match pattern {pattern!r}")
        return v


def _passes_format(value: str, pattern: Optional[str]) -> bool:
    try:
        _FormatCheck(pattern=pattern, value=value)
        return True
    except ValidationError:
        return False


def _check_format(field: TemplateField, stripped_text: str) -> list[str]:
    if field.data_type == "enum" and field.validation_regex is None:
        return ["enum_missing_regex"]
    if not _passes_format(stripped_text, field.validation_regex):
        return ["format_invalid"]
    return []


def _check_confidence(aggregate_confidence: float, confidence_floor: float) -> list[str]:
    # confidence_floor is 0.0-1.0; aggregate_confidence is Tesseract's
    # native 0-100 scale - the *100 here is deliberate, not a rounding nicety.
    if aggregate_confidence < confidence_floor * 100:
        return ["low_confidence"]
    return []


def validate_field(field: TemplateField, extraction: FieldExtractionResult) -> FieldValidationResult:
    # OCR routinely adds stray leading/trailing whitespace that would
    # otherwise fail an already-correct read - stripped only for the
    # comparison, never for what's stored/returned as `value`.
    stripped_text = extraction.raw_text.strip()

    reasons = _check_format(field, stripped_text) + _check_confidence(
        extraction.aggregate_confidence, field.confidence_floor
    )

    return FieldValidationResult(
        field_id=field.id,
        value=extraction.raw_text,
        confidence=extraction.aggregate_confidence,
        accepted=not reasons,
        reasons=reasons,
    )


def validate_fields(template: TemplateOut, extractions: dict[str, FieldExtractionResult]) -> ValidationSummary:
    """Runs validate_field for every field in `template`, pre-partitioned
    into accepted/flagged so Module 8 doesn't have to re-derive the split."""
    accepted: dict[str, FieldValidationResult] = {}
    flagged: dict[str, FieldValidationResult] = {}

    for field in template.fields:
        result = validate_field(field, extractions[field.id])
        if result.accepted:
            accepted[field.id] = result
        else:
            flagged[field.id] = result

    return ValidationSummary(accepted=accepted, flagged=flagged)
