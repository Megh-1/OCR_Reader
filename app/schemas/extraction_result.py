from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class FieldResult(BaseModel):
    field_id: str
    value: str
    confidence: float
    needs_review: bool
    reasons: list[str]
    # What Module 7 decided at extraction time: "auto", "spot_check" (accepted but
    # under CONFIDENCE_AUTO_APPROVE_THRESHOLD, or with no regex/checksum to check it
    # by) or "review" (rejected). A reviewer
    # resolving a flagged field clears needs_review but leaves this as the machine's
    # own call. None only on results stored before tiers existed.
    approval_tier: Optional[Literal["auto", "spot_check", "review"]] = None
    # Set when a field that would have been a spot check was approved as `auto` because a person has checked
    # enough of this field's values to show its error rate is under the target (app/modules/trust.py).
    auto_basis: Optional[Literal["trusted_field"]] = None
    # Who last settled this value: "person" (confirmed or corrected it on its own) or "bulk" (accepted with a group or with the
    # agreed values, an unseen accept). None = nobody has, it stands on the machine's read alone. The JSON export reads this.
    verified_by: Optional[Literal["person", "bulk"]] = None
    # Set for a value read by the page engines (app/modules/page_tiers.py); None on template-path reads and on results stored before.
    engines: Optional[list[str]] = None  # the engines that gave this value (the primary first, then those that agreed)
    source_text: Optional[str] = None  # the words on the page the primary engine says it took the value from
    grounded: Optional[bool] = None  # the value is on the page (text layer, or a fuzzy match with the page's own OCR words)
    agreed: Optional[bool] = None  # a second reader gave the same value; False: readers disagree; None: no second reader read it


class CompiledResult(BaseModel):
    template_id: str  # when template_matched is False, the base schema that judged the page, not a template the page matched
    version: int
    template_matched: bool = True
    extracted_at: datetime
    overall_status: Literal["complete", "needs_review"]
    fields: dict[str, FieldResult]


class FieldValueUpdate(BaseModel):
    """PATCH /results/{id}/fields/{field_id} body: a direct correction from the Results
    screen, independent of whether a review-queue entry exists for this field."""

    value: str
