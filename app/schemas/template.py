from datetime import date
from typing import Literal, Optional

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
    # A real check-digit algorithm Module 7 applies ON TOP of the regex. A regex
    # can only say a value is shaped right; a checksum catches a value that is
    # shaped right and wrong. Restricted to known names so a typo fails when the
    # template is saved instead of silently checking nothing. Only add a name
    # here for an algorithm the domain actually defines.
    checksum: Optional[Literal["iata_awb_mod7"]] = None

    _check_bbox = field_validator("bbox")(_validate_bbox)


class TemplateHints(BaseModel):
    """
    What Module 2 would need to know about a document to process it well, but
    cannot know on its own: Module 2 runs before Module 5 has identified
    anything. The pipeline resolves this by preprocessing twice - once
    generically to identify the type, then again with that type's hints
    applied - so these are per-document-type preprocessing parameters, not
    extraction rules.

    Both default to "behave as before", so a template that omits them is
    unaffected.
    """

    page_aspect_ratio: Optional[float] = PydField(default=None, gt=0.0)
    """
    True width/height of the physical page (e.g. 612/792 for US Letter).

    A perspective warp has to guess output dimensions from the quadrilateral's
    measured edge lengths, and those edges are foreshortened by the very
    perspective being corrected - so the recovered page comes out with an
    aspect ratio off by several percent, which shifts every relative bbox
    sideways. Knowing the real ratio removes the guess.
    """

    ruled_form: bool = False
    """
    Whether this document is a ruled grid form (air waybill, invoice table,
    passbook) whose printed cell borders must be erased before OCR.

    Not a global setting because it is genuinely document-specific: erasing
    rules is the largest single accuracy gain on a grid form, and measurably
    harmful on a document that has no rules, where the same morphology clips
    glyph strokes instead.
    """


class TemplateIn(BaseModel):
    """POST /templates request body. version and created_at are server-assigned, never client-supplied."""

    template_id: str
    anchors: list[TemplateAnchor] = PydField(min_length=1)
    fields: list[TemplateField] = PydField(default_factory=list)
    hints: TemplateHints = PydField(default_factory=TemplateHints)


class TemplateOut(BaseModel):
    """The versioned record shape from reference doc section 6."""

    template_id: str
    version: int
    anchors: list[TemplateAnchor]
    fields: list[TemplateField]
    created_at: date
    # Defaulted so template versions written before hints existed still load.
    hints: TemplateHints = PydField(default_factory=TemplateHints)
