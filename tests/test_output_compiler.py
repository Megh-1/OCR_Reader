from app.db import SessionLocal
from app.models.extraction_result import ExtractionResultRecord
from app.modules.output_compiler import compile_output, persist_result
from app.modules.validation import FieldValidationResult, ValidationSummary


def make_result(
    field_id: str, value: str = "text", confidence: float = 90.0, accepted: bool = True, reasons: list[str] | None = None
) -> FieldValidationResult:
    return FieldValidationResult(field_id=field_id, value=value, confidence=confidence, accepted=accepted, reasons=reasons or [])


def test_all_accepted_gives_complete_status():
    summary = ValidationSummary(accepted={"a": make_result("a", value="Alice", confidence=95.0)}, flagged={})

    result = compile_output("doc_type_x", 1, summary)

    assert result.overall_status == "complete"


def test_one_flagged_field_marks_needs_review_without_hiding_accepted():
    summary = ValidationSummary(
        accepted={"a": make_result("a", value="Alice", confidence=95.0)},
        flagged={"b": make_result("b", value="???", confidence=40.0, accepted=False, reasons=["low_confidence"])},
    )

    result = compile_output("doc_type_x", 1, summary)

    assert result.overall_status == "needs_review"
    # The flag must not hide or drop the accepted field.
    assert "a" in result.fields
    assert result.fields["a"].value == "Alice"
    assert result.fields["a"].needs_review is False
    assert "b" in result.fields
    assert result.fields["b"].needs_review is True
    assert result.fields["b"].reasons == ["low_confidence"]


def test_needs_review_matches_bucket_origin_for_each_field():
    summary = ValidationSummary(
        accepted={"a1": make_result("a1"), "a2": make_result("a2")},
        flagged={"f1": make_result("f1", accepted=False, reasons=["format_invalid"])},
    )

    result = compile_output("doc_type_x", 1, summary)

    assert result.fields["a1"].needs_review is False
    assert result.fields["a2"].needs_review is False
    assert result.fields["f1"].needs_review is True


def test_persist_result_round_trips_including_flagged_reasons(unique_doc_type):
    summary = ValidationSummary(
        accepted={"a": make_result("a", value="Alice", confidence=95.0)},
        flagged={
            "b": make_result("b", value="bad!", confidence=40.0, accepted=False, reasons=["format_invalid", "low_confidence"])
        },
    )
    compiled = compile_output(unique_doc_type, 3, summary)

    result_id = persist_result(compiled)

    # Query back directly - not trusting the returned id alone.
    session = SessionLocal()
    try:
        row = session.get(ExtractionResultRecord, result_id)
        assert row is not None
        assert row.template_id == unique_doc_type
        assert row.template_version == 3
        assert row.overall_status == "needs_review"
        assert row.fields["a"]["value"] == "Alice"
        assert row.fields["a"]["needs_review"] is False
        assert row.fields["b"]["reasons"] == ["format_invalid", "low_confidence"]
        assert row.fields["b"]["needs_review"] is True
    finally:
        session.close()


def test_get_result_returns_persisted_record(client, unique_doc_type):
    summary = ValidationSummary(accepted={"a": make_result("a", value="Alice", confidence=95.0)}, flagged={})
    compiled = compile_output(unique_doc_type, 2, summary)
    result_id = persist_result(compiled)

    resp = client.get(f"/results/{result_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["template_id"] == unique_doc_type
    assert body["version"] == 2
    assert body["overall_status"] == "complete"
    assert body["fields"]["a"]["value"] == "Alice"


def test_get_result_404s_for_unknown_id(client):
    resp = client.get("/results/999999999")
    assert resp.status_code == 404


def test_zero_fields_does_not_crash_and_is_complete(unique_doc_type):
    summary = ValidationSummary(accepted={}, flagged={})

    result = compile_output(unique_doc_type, 1, summary)

    assert result.fields == {}
    # Decision: with nothing flagged (vacuously true when there's nothing
    # at all), there is nothing to review, so this is "complete" - the same
    # rule as every other case, no special-casing for zero fields.
    assert result.overall_status == "complete"
