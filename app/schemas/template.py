from datetime import date
from typing import Optional

from pydantic import BaseModel
from pydantic import Field as PydField
from pydantic import field_validator

BBox = tuple[float, float, float, float]


def _validate_bbox(v: BBox) -> BBox:
    """
    Shared bbox check for anchors and fields alike. Coordinates are relative
    % of page dimensions (0.0-1.0), never pixels — this is what the reference
    doc calls out as "never trusted blindly" from the client, so it's
    re-derived here server-side regardless of what the designer UI computed.
    """
    x1, y1, x2, y2 = v
    for name, coord in zip(("x1", "y1", "x2", "y2"), v):
        if not (0.0 <= coord <= 1.0):
            raise ValueError(f"bbox {name}={coord} must be in [0.0, 1.0]")
    if not x1 < x2:
        raise ValueError(f"bbox x1 ({x1}) must be < x2 ({x2})")
    if not y1 < y2:
        raise ValueError(f"bbox y1 ({y1}) must be < y2 ({y2})")
    return v


class TemplateAnchor(BaseModel):
    text: str
    bbox: BBox
    match_threshold: float = PydField(ge=0.0, le=1.0)

    _check_bbox = field_validator("bbox")(_validate_bbox)


class TemplateField(BaseModel):
    id: str
    label: str
    bbox: BBox
    data_type: str
    validation_regex: Optional[str] = None
    confidence_floor: float = PydField(ge=0.0, le=1.0)

    _check_bbox = field_validator("bbox")(_validate_bbox)


class TemplateIn(BaseModel):
    """POST /templates request body. version and created_at are server-assigned, never client-supplied."""

    template_id: str
    anchors: list[TemplateAnchor] = PydField(min_length=1)
    fields: list[TemplateField] = PydField(default_factory=list)


class TemplateOut(BaseModel):
    """The versioned record shape from reference doc section 6."""

    template_id: str
    version: int
    anchors: list[TemplateAnchor]
    fields: list[TemplateField]
    created_at: date
