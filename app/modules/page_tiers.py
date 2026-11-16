"""
Tiers for a page read by page engines (sprint Step 4). The thresholds and rules are the existing ones; nothing is lowered or loosened.

For every field of the PRIMARY page engine's read:

  grounded   the value is on the page: in the text layer (a digital page) or fuzzy >= 90 against the whole-page Tesseract words (app/modules/grounding.py)
  rules      the field's own regex, the AWB mod-7 check digit and the template's cross-field checks, run by validation.validate_fields (a confidence
             of 100 is passed in so only the format, check-digit and cross-check findings decide; the page engines report no confidence)
  agreement  a second independent reader gives the same value, normalised as the scorer does (app/utils/compare.py): the other page engine, or a
             crop engine (our own adapter or Tesseract / the text layer) when a template matched

  auto        = grounded AND rules AND agreement (and the value is not blank: nothing to verify)
  spot_check  = rules pass but grounding or agreement is missing
  review      = a rule fails, or the readers that did read something disagree (none of them equals the primary value)

A blank value is never auto. With no second reader at all (one page engine and no crop engine) nothing is auto and every field says so.
Only adds caution on top of validation: a field the rules reject is never promoted.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Mapping, Optional

from app.modules.field_extraction import FieldExtractionResult
from app.modules.grounding import PageText
from app.modules.validation import validate_fields
from app.utils.compare import values_agree


@dataclass
class TieredField:
    field_id: str
    value: str
    source_text: str
    engines: list[str]  # the engines that gave this value (the primary first, then those that agreed with it)
    grounded: bool
    agreed: Optional[bool]  # True: a second reader gave the same value; False: readers disagree; None: no second reader read the field
    tier: str
    reasons: list[str] = dc_field(default_factory=list)


def tier_fields(template, primary: Mapping[str, tuple[str, str]], primary_engine: str, others: Mapping[str, Mapping[str, str]], page: PageText) -> dict[str, TieredField]:
    """`primary`: {field id: (value, source_text)}; `others`: {engine name: {field id: value}} for the second readers (page engines and/or a crop engine).
    `template` is anything with .fields and .checks (a TemplateOut or the seed's TemplateIn)."""
    extractions = {
        f.id: FieldExtractionResult(field_id=f.id, raw_text=primary.get(f.id, ("", ""))[0], word_confidences=[], aggregate_confidence=100.0, psm_used=0, source="page")
        for f in template.fields
    }
    summary = validate_fields(template, extractions)
    rule_findings = {fid: r.reasons for fid, r in summary.flagged.items()}
    out: dict[str, TieredField] = {}
    for f in template.fields:
        value, source = primary.get(f.id, ("", ""))
        grounded = bool(value.strip()) and page.grounded(value)
        same = [name for name, vals in others.items() if value.strip() and values_agree(f.id, vals.get(f.id, ""), value)]
        read_something = [name for name, vals in others.items() if (vals.get(f.id) or "").strip()]
        if not others:
            agreed = None
        elif same:
            agreed = True
        elif read_something:
            agreed = False
        else:
            agreed = None
        reasons = list(rule_findings.get(f.id, []))
        if reasons:
            tier = "review"
        elif not value.strip():
            tier, reasons = "spot_check", ["blank"]
        elif agreed is False:
            tier, reasons = "review", ["readers_disagree"]
        elif grounded and agreed:
            tier = "auto"
        else:
            tier = "spot_check"
            if not grounded:
                reasons.append("not_grounded")
            if not others:
                reasons.append("single_reader")
            elif not agreed:
                reasons.append("no_second_reader_value")
        out[f.id] = TieredField(field_id=f.id, value=value, source_text=source, engines=[primary_engine, *same], grounded=grounded, agreed=agreed, tier=tier, reasons=reasons)
    return out
