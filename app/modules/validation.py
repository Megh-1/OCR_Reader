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

A field may also name a `checksum` - a real check-digit algorithm. That gets its
own reason, "check_digit_invalid", deliberately distinct from "format_invalid":
the two catch different classes of error. A malformed value is caught by the
regex; a value with the right shape and one wrong digit (176-06067726 for
176-06067736) sails through any regex and is exactly what a checksum is for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Callable, Literal, Optional

from pydantic import BaseModel, ValidationError, field_validator

from app.config import CONFIDENCE_AUTO_APPROVE_THRESHOLD
from app.modules.checks import CheckOutcome, evaluate_checks
from app.modules.field_extraction import FieldExtractionResult
from app.schemas.template import TemplateField, TemplateOut


ApprovalTier = Literal["auto", "spot_check", "review"]


@dataclass
class FieldValidationResult:
    field_id: str
    value: str  # the ORIGINAL raw_text, unstripped - stripping is only for the comparison
    confidence: float
    accepted: bool
    reasons: list[str] = dataclass_field(default_factory=list)  # empty when accepted
    # None only for a result built by hand without going through validate_field.
    approval_tier: Optional[ApprovalTier] = None


@dataclass
class ValidationSummary:
    accepted: dict[str, FieldValidationResult]
    flagged: dict[str, FieldValidationResult]
    # Every cross-field check that ran, passed / failed / skipped, for reporting and tests.
    checks: list[CheckOutcome] = dataclass_field(default_factory=list)


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


def _iata_awb_mod7(value: str) -> Optional[bool]:
    """
    IATA air waybill check: 3-digit airline prefix, then a 7-digit serial, then
    ONE check digit, which must equal the serial mod 7. So 176-06067736 has
    serial 0606773, 606773 mod 7 = 6, check digit 6: valid. 176-06067726 has
    serial 0606772, mod 7 = 5, check digit 6: not.

    Returns None - not True, not False - for a value that is not 11 digits once
    hyphens and spaces are removed. The check cannot judge what it cannot parse,
    and a malformed value is the regex's finding to report; reporting it here
    too would blur the line between "malformed" and "plausible but wrong".
    Consequently a checksum field should also carry a regex, or a malformed
    value would pass both.

    Known blind spot: 0 and 7 differ by 7, so misreading one for the other in
    the serial leaves the remainder unchanged and the check passes. Every other
    single-digit substitution in the serial is caught, and any change to the
    check digit is, since it can only be 0-6.
    """
    digits = re.sub(r"[\s-]", "", value)
    if not re.fullmatch(r"\d{11}", digits):
        return None
    serial, check_digit = int(digits[3:10]), int(digits[10])
    return serial % 7 == check_digit


CHECKSUMS: dict[str, Callable[[str], Optional[bool]]] = {
    "iata_awb_mod7": _iata_awb_mod7,
}


def _check_checksum(field: TemplateField, stripped_text: str) -> list[str]:
    if field.checksum is None:
        return []
    return ["check_digit_invalid"] if CHECKSUMS[field.checksum](stripped_text) is False else []


def _check_confidence(aggregate_confidence: float, confidence_floor: float) -> list[str]:
    # confidence_floor is 0.0-1.0; aggregate_confidence is Tesseract's
    # native 0-100 scale - the *100 here is deliberate, not a rounding nicety.
    if aggregate_confidence < confidence_floor * 100:
        return ["low_confidence"]
    return []


def has_validator(field: TemplateField) -> bool:
    """The template gives the field something besides confidence to be judged by: a regex or a checksum."""
    return field.validation_regex is not None or field.checksum is not None


def approval_tier(accepted: bool, aggregate_confidence: float, has_validator: bool) -> ApprovalTier:
    """
    How much human attention an ACCEPTED-or-not field still gets.

      review      Module 7 rejected it (format, check digit or confidence floor).
      spot_check  accepted, but under CONFIDENCE_AUTO_APPROVE_THRESHOLD - or the
                  template gives it no regex and no checksum, however confident.
      auto        accepted, validated by a regex or checksum, and at or above the threshold.

    A field with no validator is never auto because confidence is then the only
    check, and confidence is exactly what a wrong read can have plenty of: every
    wrong value Module 7 accepted on the synthetic corpus was in such a field. That
    includes a text-native field, whose fixed 100 (TEXT_LAYER_CONFIDENCE) says the
    PDF held those words, not that the box caught the right ones. The rule trusts the
    template author: a regex that admits anything still counts as one.

    The tier only ever ADDS caution on top of validation: a rejected field is never
    promoted, and neither the threshold nor a missing validator can make an accepted
    field "review". "At or above" - a field exactly on the line is auto - matches how
    the per-field floor treats its own line.
    """
    if not accepted:
        return "review"
    if not has_validator:
        return "spot_check"
    return "auto" if aggregate_confidence >= CONFIDENCE_AUTO_APPROVE_THRESHOLD * 100 else "spot_check"


def validate_field(field: TemplateField, extraction: FieldExtractionResult) -> FieldValidationResult:
    # OCR routinely adds stray leading/trailing whitespace that would
    # otherwise fail an already-correct read - stripped only for the
    # comparison, never for what's stored/returned as `value`.
    stripped_text = extraction.raw_text.strip()

    if field.optional and not stripped_text:
        # A derived field whose source was read and held no such value (Module 6 gave it the
        # source's confidence; a source that read nothing arrives at 0 and fails the floor). There
        # is nothing to format-check, but a blank is NEVER auto - not even from exact PDF text. The text
        # proves the pattern did not match, not that the value is absent ("HS CODE NO. 61099020" misses a
        # pattern that expects digits after CODE), and a scan cannot tell an absent marker from a garbled
        # one. A field that cannot be verified is never auto however confident (CLAUDE.md, Module 7).
        reasons = _check_confidence(extraction.aggregate_confidence, field.confidence_floor)
        tier = "review" if reasons else "spot_check"
        return FieldValidationResult(
            field_id=field.id, value=extraction.raw_text, confidence=extraction.aggregate_confidence,
            accepted=not reasons, reasons=reasons, approval_tier=tier,
        )

    reasons = (
        _check_format(field, stripped_text)
        + _check_checksum(field, stripped_text)
        + _check_confidence(extraction.aggregate_confidence, field.confidence_floor)
    )

    return FieldValidationResult(
        field_id=field.id,
        value=extraction.raw_text,
        confidence=extraction.aggregate_confidence,
        accepted=not reasons,
        reasons=reasons,
        approval_tier=approval_tier(not reasons, extraction.aggregate_confidence, has_validator(field)),
    )


def demote_auto(summary: ValidationSummary) -> int:
    """
    Caps every accepted field at spot_check and returns how many were auto. For a page read by a weaker reader than its template
    asks for (the vision reader was meant to read it and Tesseract had to): confidence and a regex from that read vouch for much
    less. Measured on hard photographs read that way, 4 of the 13 values that reached auto were wrong (`1788.00` for `391.00`,
    `7480` for `74803.00`, and two that lost a trailing digit), at confidence 93-96. Only ever adds caution: a rejected field stays
    rejected and nothing is promoted.
    """
    demoted = 0
    for result in summary.accepted.values():
        if result.approval_tier == "auto":
            result.approval_tier = "spot_check"
            demoted += 1
    return demoted


def demote_fields(summary: ValidationSummary, field_ids) -> int:
    """Caps the named accepted fields at spot_check and returns how many were auto (see demote_auto). For values read through a
    registration shift of the text layer (layer_registration.py): the shift is chosen by geometry and format, not by anything that
    proves the cell, so such a value is at most a spot check."""
    demoted = 0
    for fid in field_ids:
        result = summary.accepted.get(fid)
        if result is not None and result.approval_tier == "auto":
            result.approval_tier = "spot_check"
            demoted += 1
    return demoted


def cross_check_reason(outcome: CheckOutcome) -> str:
    """The reason string a failed check adds to each field it names (the label is shown to a reviewer)."""
    return f"cross_check_failed:{outcome.label}"


def validate_fields(template: TemplateOut, extractions: dict[str, FieldExtractionResult]) -> ValidationSummary:
    """Runs validate_field for every field in `template`, pre-partitioned
    into accepted/flagged so Module 8 doesn't have to re-derive the split.

    Then the template's cross-field checks run on what was read. A failed check flags EVERY field
    it names - including one that passed its own checks, since a value can look right alone and
    still disagree with the others - with its reason added. That only ever adds caution: a field
    already flagged stays flagged and just gains a reason; nothing is promoted or edited."""
    accepted: dict[str, FieldValidationResult] = {}
    flagged: dict[str, FieldValidationResult] = {}

    for field in template.fields:
        result = validate_field(field, extractions[field.id])
        if result.accepted:
            accepted[field.id] = result
        else:
            flagged[field.id] = result

    outcomes = evaluate_checks(template.checks, {f.id: extractions[f.id].raw_text for f in template.fields})
    for outcome in outcomes:
        if outcome.status != "failed":
            continue
        reason = cross_check_reason(outcome)
        for field_id in outcome.fields:
            if field_id in accepted:
                was = accepted.pop(field_id)
                flagged[field_id] = FieldValidationResult(
                    field_id=was.field_id, value=was.value, confidence=was.confidence,
                    accepted=False, reasons=[reason], approval_tier="review",
                )
            elif field_id in flagged and reason not in flagged[field_id].reasons:
                flagged[field_id].reasons.append(reason)

    return ValidationSummary(accepted=accepted, flagged=flagged, checks=outcomes)
