from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class FieldResult(BaseModel):
    field_id: str
    value: str
    confidence: float
    needs_review: bool
    reasons: list[str]


class CompiledResult(BaseModel):
    template_id: str
    version: int
    extracted_at: datetime
    overall_status: Literal["complete", "needs_review"]
    fields: dict[str, FieldResult]
