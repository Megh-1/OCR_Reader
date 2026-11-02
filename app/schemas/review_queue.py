from datetime import datetime
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter


class FieldReviewPayload(BaseModel):
    entry_type: Literal["field_review"] = "field_review"
    extraction_result_id: int
    field_id: str
    original_value: str
    reasons: list[str]
    confidence: Optional[float] = None  # None on entries written before it was recorded


class SpotCheckPayload(BaseModel):
    """A field Module 7 ACCEPTED but did not auto-approve: its approval_tier is spot_check.
    `why` says which rule held it back - confidence under CONFIDENCE_AUTO_APPROVE_THRESHOLD,
    or no regex/checksum at all, in which case confidence was the only thing that judged it."""

    entry_type: Literal["spot_check"] = "spot_check"
    extraction_result_id: int
    field_id: str
    original_value: str
    confidence: float
    why: Literal["below_auto_threshold", "no_validator", "blank_optional"]
    # Held back from a bulk accept as a random sample, so the group's accepted values are measured, not just trusted.
    audit: bool = False


class NewTemplateNeededPayload(BaseModel):
    entry_type: Literal["new_template_needed"] = "new_template_needed"
    attempted_type_hint: Optional[str] = None
    rejection_reason: str  # one of Module 5's "no_match" | "ambiguous_match" | "unknown_type_hint"


# entry_type is embedded in the stored JSONB itself (redundant with the
# row's own entry_type column) so payload is self-describing on its own,
# the same pattern Module 8 already uses (each field in its `fields` JSONB
# carries its own field_id even though it's also the dict key). That's
# what a discriminated union needs to validate on: the discriminator field
# must actually be present in the data being validated, not just implied
# by which table row it came from.
ReviewPayload = Annotated[
    Union[FieldReviewPayload, SpotCheckPayload, NewTemplateNeededPayload], Field(discriminator="entry_type")
]
ReviewPayloadAdapter: TypeAdapter = TypeAdapter(ReviewPayload)


class FieldReviewResolution(BaseModel):
    entry_type: Literal["field_review"] = "field_review"
    confirmed_value: str
    via: Literal["single", "bulk"] = "single"  # a bulk accept is not an independent check of that value


class SpotCheckResolution(BaseModel):
    entry_type: Literal["spot_check"] = "spot_check"
    confirmed_value: str
    via: Literal["single", "bulk"] = "single"


class NewTemplateNeededResolution(BaseModel):
    entry_type: Literal["new_template_needed"] = "new_template_needed"
    created_template_id: str


ReviewResolution = Annotated[
    Union[FieldReviewResolution, SpotCheckResolution, NewTemplateNeededResolution], Field(discriminator="entry_type")
]
ReviewResolutionAdapter: TypeAdapter = TypeAdapter(ReviewResolution)


class ReviewQueueEntry(BaseModel):
    """The full row shape, for listing/inspection - payload/resolution are
    typed via the discriminated unions above, not loose dicts."""

    model_config = {"from_attributes": True}

    id: int
    entry_type: str
    status: str
    created_at: datetime
    resolved_at: Optional[datetime] = None
    payload: ReviewPayload
    resolution: Optional[ReviewResolution] = None


class ReviewResolutionRequest(BaseModel):
    """PATCH /review/{id} body. Exactly one of these two is required,
    depending on the row's own entry_type - the endpoint figures out which,
    the caller doesn't specify which kind of entry this is."""

    confirmed_value: Optional[str] = None
    created_template_id: Optional[str] = None
