"""
Module 8 - Output Compiler.

Assembles Module 7's ValidationSummary into the final artifact and persists
it. Deliberately stops there: no review-queue enqueueing happens here.
Module 9 doesn't exist yet, and stubbing a fake queue table or a
placeholder call to a module that isn't built would be scope creep this
module doesn't need. A flagged field means the record's overall_status
becomes "needs_review" - it does NOT mean the accepted fields get dropped
or hidden; every field from Module 7's output (accepted or flagged) is
carried through, since Module 7 already guarantees an exhaustive,
non-overlapping partition and re-deriving or second-guessing that split
here is not this module's job.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app.db import SessionLocal
from app.models.extraction_result import ExtractionResultRecord
from app.modules.validation import ValidationSummary
from app.schemas.extraction_result import CompiledResult, FieldResult

router = APIRouter()


def compile_output(template_id: str, version: int, validation_summary: ValidationSummary) -> CompiledResult:
    fields: dict[str, FieldResult] = {}

    for field_id, result in validation_summary.accepted.items():
        fields[field_id] = FieldResult(
            field_id=result.field_id, value=result.value, confidence=result.confidence,
            needs_review=False, reasons=result.reasons,
        )
    for field_id, result in validation_summary.flagged.items():
        fields[field_id] = FieldResult(
            field_id=result.field_id, value=result.value, confidence=result.confidence,
            needs_review=True, reasons=result.reasons,
        )

    # A single flagged field is enough to mark the whole record for review -
    # note this rule alone also covers the zero-fields edge case: an empty
    # .flagged dict (whether or not anything was accepted either) means
    # "complete", since there is nothing to review by definition.
    overall_status = "needs_review" if validation_summary.flagged else "complete"

    return CompiledResult(
        template_id=template_id,
        version=version,
        extracted_at=datetime.now(timezone.utc),
        overall_status=overall_status,
        fields=fields,
    )


def persist_result(compiled_result: CompiledResult) -> int:
    """
    Inserts one log-entry row. Uses Pydantic's typed serialization
    (model_dump(mode="json")) rather than manually re-building the fields
    dict by hand - model_dump_json() specifically would hand SQLAlchemy's
    JSONB column a JSON-encoded *string*, which a JSONB bind parameter
    would store as a quoted string value rather than a real nested object;
    model_dump(mode="json") produces the equivalent JSON-safe Python dict
    that a JSONB column actually expects.
    """
    fields_json = compiled_result.model_dump(mode="json")["fields"]

    with SessionLocal() as session:
        record = ExtractionResultRecord(
            template_id=compiled_result.template_id,
            template_version=compiled_result.version,
            extracted_at=compiled_result.extracted_at,
            overall_status=compiled_result.overall_status,
            fields=fields_json,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.id


def _record_to_compiled_result(record: ExtractionResultRecord) -> CompiledResult:
    # Re-validates the JSONB payload back into typed FieldResult objects on
    # the way out - schema-checked on read, not just on write.
    return CompiledResult(
        template_id=record.template_id,
        version=record.template_version,
        extracted_at=record.extracted_at,
        overall_status=record.overall_status,
        fields=record.fields,
    )


@router.get("/results/{result_id}", response_model=CompiledResult)
def get_result(result_id: int) -> CompiledResult:
    with SessionLocal() as session:
        record = session.get(ExtractionResultRecord, result_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No result found for id {result_id}")
    return _record_to_compiled_result(record)
