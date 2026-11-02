import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel
from pydantic import Field as PydField
from pydantic import field_validator, model_validator

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


class TemplateAnchorIn(TemplateAnchor):
    """Write-side anchor. An empty anchor text scores 100 (rapidfuzz: '' vs '') against any
    crop OCR reads as blank, so it "verifies" empty paper; refuse it when saving. Reads
    (TemplateOut) stay on the plain TemplateAnchor so templates saved before this rule still
    load instead of breaking Module 5's scan of every known type."""

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("anchor text must not be empty: type the printed label this box covers")
        return v


class TemplateLandmark(BaseModel):
    """
    A printed word that sits at the same place on every document of this layout, with that place
    as page fractions (x of its left edge, y of its vertical centre). Module 5 and Module 6 find
    these words again on the page and move every box by the shift and stretch they imply
    (app/modules/alignment.py). `text` is lower-case letters only. Built by
    scripts/build_landmarks.py from words that recur across real documents, never typed by hand.
    """

    text: str
    x: float = PydField(ge=0.0, le=1.0)
    y: float = PydField(ge=0.0, le=1.0)


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

    hint_only: bool = False
    """
    Skip this template in Module 5's no-hint scan; it is used only when an upload
    names it as its type_hint. For a template that reads part of a layout another
    template already covers: both would pass the same anchors, every un-hinted
    upload would come back ambiguous_match, and the scan would stop working for the
    full template too. A hint still has to pass this template's own anchors.
    """

    is_part: bool = False
    """
    This template reads only PART of a layout (for example the shipper block of a waybill). It is its own
    template and refers to no other. Left to automatic detection (hint_only False) it must carry at least two
    anchors: one shared printed label would let it claim pages that belong to the whole form.
    """

    min_anchors_matching: Optional[int] = PydField(default=None, ge=2)
    """
    Recognise a page when at least this many of the template's anchors match, instead of all of them. For a form whose
    printed labels are sometimes smudged, cut off or misread. Unset (the default) keeps the strict rule: every anchor must
    match. At least 2, and no more than the template has: one matching label alone never recognises a page.
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


CheckKind = Literal["product", "sum", "equal", "one_of", "maps_to"]
_CHECK_ID = re.compile(r"^[a-z][a-z0-9_]*$")


class CheckCondition(BaseModel):
    """Run a check only when `field` reads exactly `equals` (case-insensitive). With the field
    missing or different the check is skipped, never failed: it does not apply."""

    field: str
    equals: str


class TemplateCheck(BaseModel):
    """
    A rule between fields, written by whoever builds the template and evaluated by Module 7
    AFTER each field's own checks (app/modules/checks.py). It never changes a value: when it
    fails it flags every field it names, so it can only add caution. It is skipped (neither
    passed nor failed) when it cannot decide - an input missing, empty or unparseable - because
    that is the field's own format check to report.

      product   inputs[0] * inputs[1] == target, within `tolerance` (absolute)
      sum       sum(inputs) == target, within `tolerance`
      equal     the two inputs agree (after `patterns`, case-insensitive)
      one_of    inputs[0] is one of `values` or the built-in `lookup` list
      maps_to   table[inputs[0]] == the target field; an input not in the table is skipped

    `patterns` maps a field id to a regex with exactly ONE capture group; the group is what is
    compared, so "EK0565/27FEB" can be compared as "EK". There is no expression language and
    nothing is evaluated as code.
    """

    id: str
    label: str
    kind: CheckKind
    inputs: list[str] = PydField(min_length=1)
    target: Optional[str] = None
    tolerance: float = PydField(default=0.01, ge=0.0)
    patterns: dict[str, str] = PydField(default_factory=dict)
    values: list[str] = PydField(default_factory=list)
    lookup: Optional[Literal["iso4217"]] = None
    table: dict[str, str] = PydField(default_factory=dict)
    when: Optional[CheckCondition] = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "TemplateCheck":
        n = len(self.inputs)
        problems = []
        if self.kind == "product" and (n != 2 or not self.target):
            problems.append("product needs exactly 2 inputs and a target")
        if self.kind == "sum" and (n < 2 or not self.target):
            problems.append("sum needs at least 2 inputs and a target")
        if self.kind == "equal" and n != 2:
            problems.append("equal needs exactly 2 inputs")
        if self.kind == "one_of" and (n != 1 or not (self.values or self.lookup)):
            problems.append("one_of needs exactly 1 input and either values or a lookup")
        if self.kind == "maps_to" and (n != 1 or not self.target or not self.table):
            problems.append("maps_to needs exactly 1 input, a target and a table")
        for field_id, pattern in self.patterns.items():
            try:
                groups = re.compile(pattern).groups
            except re.error as exc:
                problems.append(f"pattern for {field_id!r} is not a valid regex: {exc}")
                continue
            if groups != 1:
                problems.append(f"pattern for {field_id!r} must have exactly one capture group, has {groups}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def field_ids(self) -> list[str]:
        """Every field this check blames when it fails (the `when` field only gates it)."""
        ids = list(self.inputs)
        if self.target:
            ids.append(self.target)
        return list(dict.fromkeys(ids))


class TemplateIn(BaseModel):
    """POST /templates request body. version and created_at are server-assigned, never client-supplied."""

    template_id: str
    anchors: list[TemplateAnchorIn] = PydField(min_length=1)
    fields: list[TemplateField] = PydField(default_factory=list)
    checks: list[TemplateCheck] = PydField(default_factory=list)
    hints: TemplateHints = PydField(default_factory=TemplateHints)
    landmarks: list[TemplateLandmark] = PydField(default_factory=list)

    @model_validator(mode="after")
    def _checks_refer_to_real_fields(self) -> "TemplateIn":
        known = {f.id for f in self.fields}
        seen: set[str] = set()
        problems = []
        if self.hints.min_anchors_matching is not None and self.hints.min_anchors_matching > len(self.anchors):
            problems.append(f"min_anchors_matching ({self.hints.min_anchors_matching}) is more than the {len(self.anchors)} anchors this template has")
        if self.hints.is_part and not self.hints.hint_only and len(self.anchors) < 2:
            problems.append("a part-template left to automatic detection needs at least 2 anchors (or tick 'only use when I choose it')")
        derived = {f.id for f in self.fields if f.from_field is not None}
        for f in self.fields:
            if f.from_field is not None and f.from_field not in known:
                problems.append(f"field {f.id!r} reads from {f.from_field!r}, which this template does not have")
            elif f.from_field in derived:
                problems.append(f"field {f.id!r} reads from {f.from_field!r}, which is itself derived (no chains)")
        for check in self.checks:
            if not _CHECK_ID.match(check.id):
                problems.append(f"check id {check.id!r} must match ^[a-z][a-z0-9_]*$")
            if check.id in seen:
                problems.append(f"duplicate check id {check.id!r}")
            seen.add(check.id)
            named = check.field_ids() + list(check.patterns) + ([check.when.field] if check.when else [])
            missing = sorted({f for f in named if f not in known})
            if missing:
                problems.append(f"check {check.id!r} names fields this template does not have: {', '.join(missing)}")
        if problems:
            raise ValueError("; ".join(problems))
        return self


class TemplateOut(BaseModel):
    """The versioned record shape from reference doc section 6."""

    template_id: str
    version: int
    anchors: list[TemplateAnchor]
    fields: list[TemplateField]
    # Defaulted so template versions written before checks existed still load.
    checks: list[TemplateCheck] = PydField(default_factory=list)
    created_at: date
    # Defaulted so template versions written before hints existed still load.
    hints: TemplateHints = PydField(default_factory=TemplateHints)
    # Defaulted so versions written before alignment existed still load; none means the boxes
    # are used exactly where they are stored, as before.
    landmarks: list[TemplateLandmark] = PydField(default_factory=list)
