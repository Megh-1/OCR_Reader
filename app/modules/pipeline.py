"""
End-to-end orchestrator: chains Modules 1 -> 2 -> 5 -> 6 -> 7 -> 8 via their
existing, already-tested public functions only. Nothing here reimplements
any module's own logic - this file's only job is the wiring between them,
plus routing both of Module 9's entry points exactly as the reference
doc's architecture diagram describes:
  - Module 5 rejects (any reason) -> enqueue_new_template_needed
  - Module 7/8 flags a field       -> one enqueue_field_review call per field
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.modules.field_extraction import extract_fields
from app.modules.ingestion import IngestionError, ingest_file
from app.modules.output_compiler import compile_output, make_review_render, persist_result
from app.modules.preprocessing import preprocess_image
from app.modules.review_queue import enqueue_field_review, enqueue_new_template_needed
from app.modules.template_store import get_template_version
from app.modules.type_identification import identify_document_type
from app.modules.validation import validate_fields
from app.schemas.extraction_result import CompiledResult

router = APIRouter()


class PageResult(BaseModel):
    page_index: int
    confirmed: bool
    reason: Optional[str] = None  # Module 5's rejection reason, set only when confirmed is False
    review_queue_id: Optional[int] = None  # new_template_needed id, set only when confirmed is False
    extraction_result_id: Optional[int] = None  # set only when confirmed is True
    result: Optional[CompiledResult] = None  # set only when confirmed is True
    field_review_queue_ids: list[int] = []  # one id per field Module 7 flagged, may be empty


class ProcessDocumentResponse(BaseModel):
    filename: str
    page_count: int
    pages: list[PageResult]


def _process_page(page_bytes: bytes, page_index: int, type_hint: Optional[str]) -> PageResult:
    processed = preprocess_image(page_bytes)

    identification = identify_document_type(processed.image_bytes, type_hint)

    if not identification.confirmed:
        queue_id = enqueue_new_template_needed(type_hint, identification.reason)
        return PageResult(page_index=page_index, confirmed=False, reason=identification.reason, review_queue_id=queue_id)

    # get_template_version, not get_latest_template - extract_fields/validate_fields
    # both need the EXACT version Module 5 just confirmed against.
    template = get_template_version(identification.template_id, identification.version)
    # field_image_bytes, not image_bytes: the binarized rendering that makes
    # anchors detectable destroys the stroke detail character recognition
    # needs (see ProcessedImage).
    extractions = extract_fields(identification.template_id, identification.version, processed.field_image_bytes)
    summary = validate_fields(template, extractions)

    compiled = compile_output(identification.template_id, identification.version, summary)
    # Store the grayscale rendering, not the binarized one - this is what a
    # human reviewer will look at, and it's also the image the field values
    # under review were actually read from.
    result_id = persist_result(compiled, page_image=make_review_render(processed.field_image_bytes))

    field_review_ids = [
        enqueue_field_review(result_id, field_id, flagged.value, flagged.reasons)
        for field_id, flagged in summary.flagged.items()
    ]

    return PageResult(
        page_index=page_index,
        confirmed=True,
        extraction_result_id=result_id,
        result=compiled,
        field_review_queue_ids=field_review_ids,
    )


@router.post("/documents/process", response_model=ProcessDocumentResponse)
async def process_document(file: UploadFile = File(...), type_hint: Optional[str] = Form(None)) -> ProcessDocumentResponse:
    buffer = await file.read()
    try:
        pages, metadata = ingest_file(buffer, filename=file.filename, type_hint=type_hint)
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail={"reason_code": exc.reason_code, "message": str(exc)})

    page_results = [_process_page(page_bytes, i, type_hint) for i, page_bytes in enumerate(pages)]

    return ProcessDocumentResponse(filename=metadata.filename, page_count=metadata.page_count, pages=page_results)
