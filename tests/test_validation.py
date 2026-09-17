from datetime import date

from app.modules.field_extraction import FieldExtractionResult
from app.modules.validation import validate_field, validate_fields
from app.schemas.template import TemplateField, TemplateOut


def make_field(
    field_id: str = "f1",
    data_type: str = "string",
    validation_regex: str | None = None,
    confidence_floor: float = 0.75,
) -> TemplateField:
    return TemplateField(
        id=field_id,
        label="Test",
        bbox=(0.1, 0.1, 0.4, 0.2),
        data_type=data_type,
        validation_regex=validation_regex,
        confidence_floor=confidence_floor,
    )


def make_extraction(field_id: str = "f1", raw_text: str = "", aggregate_confidence: float = 90.0) -> FieldExtractionResult:
    return FieldExtractionResult(
        field_id=field_id,
        raw_text=raw_text,
        word_confidences=[aggregate_confidence],
        aggregate_confidence=aggregate_confidence,
        psm_used=7,
    )


def test_matching_regex_and_sufficient_confidence_is_accepted():
    field = make_field(validation_regex=r"^[A-Za-z ]+$", confidence_floor=0.75)
    extraction = make_extraction(raw_text="  John Doe  ", aggregate_confidence=90.0)

    result = validate_field(field, extraction)

    assert result.accepted is True
    assert result.reasons == []
    assert result.value == "  John Doe  "  # original, unstripped, kept for audit


def test_non_matching_regex_is_flagged():
    field = make_field(validation_regex=r"^[0-9]+$", confidence_floor=0.5)
    extraction = make_extraction(raw_text="not a number", aggregate_confidence=95.0)

    result = validate_field(field, extraction)

    assert result.accepted is False
    assert "format_invalid" in result.reasons


def test_null_regex_always_passes_format_outcome_depends_on_confidence():
    field = make_field(validation_regex=None, confidence_floor=0.75)

    high_conf = validate_field(field, make_extraction(raw_text="anything at all", aggregate_confidence=90.0))
    assert high_conf.accepted is True
    assert high_conf.reasons == []

    low_conf = validate_field(field, make_extraction(raw_text="anything at all", aggregate_confidence=50.0))
    assert low_conf.accepted is False
    assert low_conf.reasons == ["low_confidence"]


def test_confidence_scale_boundary_proves_times_100_conversion():
    field = make_field(validation_regex=None, confidence_floor=0.75)

    just_above = validate_field(field, make_extraction(aggregate_confidence=76))
    assert just_above.accepted is True

    just_below = validate_field(field, make_extraction(aggregate_confidence=74))
    assert just_below.accepted is False
    assert just_below.reasons == ["low_confidence"]


def test_field_failing_both_checks_reports_both_reasons_no_short_circuit():
    field = make_field(validation_regex=r"^[0-9]+$", confidence_floor=0.9)
    extraction = make_extraction(raw_text="not a number", aggregate_confidence=50.0)

    result = validate_field(field, extraction)

    assert result.accepted is False
    assert "format_invalid" in result.reasons
    assert "low_confidence" in result.reasons
    assert len(result.reasons) == 2


def test_enum_field_with_regex_validates_like_string_field():
    field = make_field(data_type="enum", validation_regex=r"^(A|B|AB|O)[+-]$", confidence_floor=0.8)

    good = validate_field(field, make_extraction(raw_text="O+", aggregate_confidence=90.0))
    assert good.accepted is True
    assert good.reasons == []

    bad = validate_field(field, make_extraction(raw_text="Z+", aggregate_confidence=90.0))
    assert bad.accepted is False
    assert "format_invalid" in bad.reasons
    assert "enum_missing_regex" not in bad.reasons


def test_enum_field_missing_regex_is_automatically_flagged():
    field = make_field(data_type="enum", validation_regex=None, confidence_floor=0.5)
    # High confidence and plausible text - would otherwise sail through;
    # only the missing regex should stop it.
    extraction = make_extraction(raw_text="O+", aggregate_confidence=99.0)

    result = validate_field(field, extraction)

    assert result.accepted is False
    assert result.reasons == ["enum_missing_regex"]


def test_whitespace_is_stripped_before_regex_match_not_before_storing_value():
    field = make_field(validation_regex=r"^[A-Za-z ]{2,60}$", confidence_floor=0.5)
    extraction = make_extraction(raw_text="  John Doe  \n", aggregate_confidence=90.0)

    result = validate_field(field, extraction)

    assert result.accepted is True
    assert result.reasons == []
    assert result.value == "  John Doe  \n"  # original preserved, not the stripped comparison copy


def test_full_run_partitions_accepted_and_flagged_exhaustively():
    template = TemplateOut(
        template_id="t1",
        version=1,
        anchors=[{"text": "X", "bbox": (0.01, 0.01, 0.2, 0.05), "match_threshold": 0.5}],
        fields=[
            {
                "id": "good",
                "label": "Good",
                "bbox": (0.1, 0.1, 0.4, 0.2),
                "data_type": "string",
                "validation_regex": r"^[A-Za-z ]+$",
                "confidence_floor": 0.7,
            },
            {
                "id": "bad_format",
                "label": "BadFormat",
                "bbox": (0.1, 0.3, 0.4, 0.4),
                "data_type": "string",
                "validation_regex": r"^[0-9]+$",
                "confidence_floor": 0.5,
            },
            {
                "id": "bad_confidence",
                "label": "BadConf",
                "bbox": (0.1, 0.5, 0.4, 0.6),
                "data_type": "string",
                "validation_regex": None,
                "confidence_floor": 0.9,
            },
        ],
        created_at=date.today(),
    )

    extractions = {
        "good": make_extraction("good", raw_text="Valid Name", aggregate_confidence=95.0),
        "bad_format": make_extraction("bad_format", raw_text="not digits", aggregate_confidence=95.0),
        "bad_confidence": make_extraction("bad_confidence", raw_text="fine text", aggregate_confidence=40.0),
    }

    summary = validate_fields(template, extractions)

    all_field_ids = {"good", "bad_format", "bad_confidence"}
    assert set(summary.accepted) | set(summary.flagged) == all_field_ids
    assert set(summary.accepted) & set(summary.flagged) == set()  # never both
    assert set(summary.accepted) == {"good"}
    assert set(summary.flagged) == {"bad_format", "bad_confidence"}
