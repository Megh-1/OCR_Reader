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


class CompiledResult(BaseModel):
    template_id: str
    version: int
    extracted_at: datetime
    overall_status: Literal["complete", "needs_review"]
    fields: dict[str, FieldResult]
