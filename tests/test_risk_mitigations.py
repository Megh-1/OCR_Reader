"""
One test per row of the reference doc's Risks & Mitigations table (section 9).

These run REAL OCR against real images rather than mocking pytesseract - a
mocked mitigation proves only that the wiring is present, not that the
mitigation actually holds. Slower than the rest of the suite, deliberately.

The real-card tests skip when tests/fixtures/ is absent: that photo is
personal data and is not committed to the repository.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.modules.preprocessing import preprocess_image
from app.modules.field_extraction import extract_fields
from app.modules.template_store import get_latest_template
from app.modules.type_identification import identify_document_type
from app.modules.validation import validate_fields
from app.schemas.template import TemplateField

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REAL_CARD = PROJECT_ROOT / "tests" / "fixtures" / "bits_goa_id_card" / "sample_01.jpg"
OTHER_DOCUMENT = PROJECT_ROOT / "frontend" / "public" / "sample-document.png"

DOC_TYPE = "bits_goa_id_card"

# Hand-verified ground truth for the real card.
TRUTH = {
    "id_number": "2024A7PS0476G",
    "date_of_birth": "17-Oct-2006",
}

needs_real_card = pytest.mark.skipif(
    not REAL_CARD.exists(), reason="real card photo not present (personal data, not committed)"
)


@pytest.fixture(scope="module", autouse=True)
def seeded_template():
    """Loads the checked-in bits_goa_id_card definition into the test database.

    These tests assert against the real template's real tuned values, so they
    seed from scripts/seed_templates.py rather than redefining it - a copy
    here could drift from what actually ships and the tests would keep
    passing against the stale copy.
    """
    from scripts.seed_templates import BITS_GOA_ID_CARD, seed

    seed([BITS_GOA_ID_CARD])
    return get_latest_template(DOC_TYPE)


def _identify(image_bytes: bytes, type_hint: str | None = DOC_TYPE):
    return identify_document_type(preprocess_image(image_bytes).image_bytes, type_hint=type_hint)


def _rescale(image_bytes: bytes, factor: float) -> bytes:
    image = Image.open(io.BytesIO(image_bytes))
    resized = image.resize((round(image.width * factor), round(image.height * factor)), Image.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, format="PNG")
    return buffer.getvalue()


def _rotate(image_bytes: bytes, degrees: float, expand: bool = False) -> bytes:
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    rotated = image.rotate(degrees, expand=expand, fillcolor=(255, 255, 255), resample=Image.BICUBIC)
    buffer = io.BytesIO()
    rotated.save(buffer, format="PNG")
    return buffer.getvalue()


def _pad(image_bytes: bytes, fraction: float) -> bytes:
    """Simulates standing further back: same document, more background."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    pad_x, pad_y = round(image.width * fraction), round(image.height * fraction)
    canvas = Image.new("RGB", (image.width + 2 * pad_x, image.height + 2 * pad_y), (255, 255, 255))
    canvas.paste(image, (pad_x, pad_y))
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()


# --- Row 1: wrong template silently applied ---------------------------------
# Mitigation: two-signal identification (hint + anchor verification) - never
# extract on anchor failure.


@needs_real_card
def test_type_hint_alone_never_authorizes_extraction():
    """A confident, explicit hint for the WRONG document type must still be
    refused by anchor verification. The hint narrows which template gets
    checked; it never substitutes for checking it."""
    result = _identify(OTHER_DOCUMENT.read_bytes(), type_hint=DOC_TYPE)

    assert result.confirmed is False
    assert result.reason == "no_match"
    # Nothing downstream can run without these - this is what "never extract
    # on anchor failure" means structurally, not just by convention.
    assert result.template_id is None
    assert result.version is None


# --- Row 2: DPI/resolution mismatch breaks coordinates ----------------------
# Mitigation: relative (%) coordinates + resolution normalization.


@needs_real_card
def test_same_document_at_half_resolution_extracts_identical_values():
    """The whole reason bboxes are stored as percentages. Halving the input
    resolution changes every absolute pixel coordinate in the image and must
    change none of the extracted values."""
    raw = REAL_CARD.read_bytes()
    template = get_latest_template(DOC_TYPE)

    full = preprocess_image(raw)
    half = preprocess_image(_rescale(raw, 0.5))
    assert half.final_size != full.final_size  # the premise: genuinely different pixel geometry

    full_values = {k: v.raw_text.strip() for k, v in extract_fields(DOC_TYPE, template.version, full.field_image_bytes).items()}
    half_values = {k: v.raw_text.strip() for k, v in extract_fields(DOC_TYPE, template.version, half.field_image_bytes).items()}

    for field_id, expected in TRUTH.items():
        assert full_values[field_id] == expected
        assert half_values[field_id] == expected


# --- Row 3: minor scan skew/misalignment ------------------------------------
# Mitigation: deskew preprocessing + small fixed crop margin.


@needs_real_card
def test_skewed_document_is_deskewed_and_still_identifies():
    """A few degrees of rotation is what a handheld photo actually looks
    like. Deskew has to put it back before anchors are cropped.

    Rotated without expanding the canvas, so this isolates skew from framing:
    an expanding rotation also pads the image, which is a separate concern
    (see test_reframing_shifts_every_coordinate_known_phase1_limit)."""
    skewed = _rotate(REAL_CARD.read_bytes(), 2.5, expand=False)

    processed = preprocess_image(skewed)
    # Deskew measured a real rotation and corrected it, rather than no-opping.
    assert abs(processed.skew_angle_corrected) > 1.0

    result = identify_document_type(processed.image_bytes, type_hint=DOC_TYPE)
    assert result.confirmed is True


@needs_real_card
def test_reframing_shifts_every_coordinate_known_phase1_limit():
    """Documents a real boundary of the Phase 1 design, so it is a known
    tested limit rather than a surprise in front of a user.

    Every bbox is a percentage of the PAGE, and nothing crops to the document
    first, so the same card photographed with more background around it puts
    every field somewhere else in relative space. Boundary detection can't
    rescue this on a photo where the card already fills (and overruns) the
    frame - there is no complete document edge to find.

    The reference doc scopes adaptive boundary handling to Phase 2. Until
    then the operational requirement is: frame new photos like the reference
    photo. Note the failure is a clean refusal, not a wrong extraction."""
    raw = REAL_CARD.read_bytes()
    padded = _pad(raw, 0.10)

    assert _identify(raw).confirmed is True
    reframed = _identify(padded)

    assert reframed.confirmed is False
    # The part that matters: it refuses rather than extracting from the wrong
    # regions. Silently returning values read from shifted boxes would be the
    # dangerous outcome.
    assert reframed.reason == "no_match"
    assert reframed.template_id is None


# --- Row 4: OCR misreads a character ----------------------------------------
# Mitigation: per-field regex validation catches format-invalid output BEFORE
# it reaches JSON.


def test_shape_only_date_regex_would_ship_a_misread_month():
    """Regression guard for a real false accept. OCR read the card's
    "17-Oct-2006" as "17-Oci-2006"; the original shape-only pattern
    (^\\d{2}-[A-Z][a-z]{2}-\\d{4}$) matched it, so a wrong value shipped as
    trusted output - the failure mode the reference doc calls more dangerous
    than an honest refusal. The month must be enumerated, not shape-checked."""
    template = get_latest_template(DOC_TYPE)
    field = next(f for f in template.fields if f.id == "date_of_birth")

    import re

    assert re.fullmatch(field.validation_regex, "17-Oct-2006")
    assert re.fullmatch(field.validation_regex, "17-Oci-2006") is None
    assert re.fullmatch(field.validation_regex, "17-Xyz-2006") is None


def test_ocr_misread_is_flagged_not_shipped(monkeypatch):
    """The mitigation end to end: a character-level misread reaches
    validation and comes out flagged, with the bad value preserved for a
    human rather than silently corrected or dropped."""
    from app.modules.field_extraction import FieldExtractionResult
    from app.schemas.template import TemplateOut

    field = TemplateField(
        id="student_name", label="Student Name", bbox=(0.1, 0.1, 0.5, 0.2),
        data_type="string", validation_regex="^[A-Za-z ]{2,60}$", confidence_floor=0.75,
    )
    template = TemplateOut(
        template_id="t", version=1, anchors=[{"text": "X", "bbox": (0.0, 0.0, 0.1, 0.1), "match_threshold": 0.85}],
        fields=[field], created_at="2026-09-19",
    )
    # Exactly what real OCR returned for this card: an 'h' read as 'i.'
    extraction = FieldExtractionResult(
        field_id="student_name", raw_text="Megh Brijesi. Shah",
        word_confidences=[84.7], aggregate_confidence=84.7, psm_used=7,
    )

    summary = validate_fields(template, {"student_name": extraction})

    assert "student_name" in summary.flagged
    assert summary.flagged["student_name"].reasons == ["format_invalid"]
    # High confidence alone must not rescue a format-invalid value.
    assert summary.flagged["student_name"].confidence > 80
    assert summary.flagged["student_name"].value == "Megh Brijesi. Shah"


# --- Row 5: legitimate document flagged as unknown (false negative) ---------
# Mitigation: tune anchor match threshold using the test set; err toward
# flagging over guessing.


@needs_real_card
def test_anchor_threshold_sits_in_the_gap_between_match_and_non_match():
    """The threshold is only defensible if there's real separation either
    side of it. Measured: the correct card scores ~86, unrelated documents
    score ~0. A threshold pinned just under the match score has no tolerance
    for lighting or angle variation and produces false negatives on
    legitimate documents."""
    template = get_latest_template(DOC_TYPE)
    threshold = template.anchors[0].match_threshold * 100

    match_scores = [s for d in _identify(REAL_CARD.read_bytes()).per_anchor_scores.values() for s in d.values()]
    other_scores = [s for d in _identify(OTHER_DOCUMENT.read_bytes()).per_anchor_scores.values() for s in d.values()]

    assert min(match_scores) > threshold, "the real card must clear its own threshold"
    assert max(other_scores) < threshold, "an unrelated document must not"
    # Slack on both sides, not a threshold balanced on a knife edge.
    assert min(match_scores) - threshold >= 10.0
    assert threshold - max(other_scores) >= 10.0
