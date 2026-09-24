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

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.models.text_layer import TextLayer
from app.modules.field_extraction import FieldExtractionResult, extract_fields, extract_fields_from_text_layer
from app.modules.ingestion import IngestionError, IngestionMetadata, extract_text_layers, ingest_file
from app.modules.output_compiler import compile_output, make_review_render, persist_result
from app.modules.preprocessing import ProcessedImage, preprocess_image
from app.modules.review_queue import enqueue_field_review, enqueue_new_template_needed
from app.modules.template_store import get_template_version
from app.modules.type_identification import identify_document_type
from app.modules.validation import validate_fields
from app.schemas.extraction_result import CompiledResult
from app.schemas.template import TemplateOut

router = APIRouter()
log = logging.getLogger(__name__)


class PageResult(BaseModel):
    page_index: int
    confirmed: bool
    reason: Optional[str] = None  # Module 5's rejection reason, set only when confirmed is False
    review_queue_id: Optional[int] = None  # new_template_needed id, set only when confirmed is False
    extraction_result_id: Optional[int] = None  # set only when confirmed is True
    result: Optional[CompiledResult] = None  # set only when confirmed is True
    field_review_queue_ids: list[int] = []  # one id per field Module 7 flagged, may be empty
    text_native: bool = False  # True when fields were read from the PDF's embedded text, with no OCR


class ProcessDocumentResponse(BaseModel):
    filename: str
    page_count: int
    pages: list[PageResult]


def preprocess_for_template(
    page_bytes: bytes, template: TemplateOut, generic: ProcessedImage, deskew: bool = True
) -> ProcessedImage:
    """
    Pass 2 of preprocessing: now that the type is known, redo it with this
    document type's own parameters. Anchor verification only needed the page
    to be findable and legible, which the generic pass 1 delivers; field
    extraction needs the page's true proportions (so percentage bboxes land on
    the right cells) and, on a grid form, its printed rules erased. Both are
    per-document-type facts pass 1 could not have known.

    Returns `generic` unchanged when the template asks for nothing, so a
    template without hints costs no second pass. Public because
    scripts/evaluate_accuracy.py must measure exactly this path - a copy of
    it there would drift from what actually ships.
    """
    hints = template.hints
    if hints.page_aspect_ratio is None and not hints.ruled_form:
        return generic
    return preprocess_image(
        page_bytes, target_aspect=hints.page_aspect_ratio, remove_rules=hints.ruled_form, deskew=deskew
    )


@dataclass
class PageExtraction:
    fields: dict[str, FieldExtractionResult]
    review_image_bytes: bytes  # the rendering a human reviewer should see for this page
    text_native: bool


def extract_page_fields(
    template: TemplateOut, page_bytes: bytes, generic: ProcessedImage, text_layer: Optional[TextLayer]
) -> PageExtraction:
    """
    Module 6 for one page, choosing its path.

    A page with an embedded text layer is read straight from it: no second
    preprocessing pass and no OCR, since the second pass exists only to make a
    raster legible to Tesseract. Every other page falls through unchanged to
    the raster path. Module 5's anchor check has already run on the raster
    either way - form labels are typically drawn as vector artwork rather than
    stored as text, so the text layer could not verify them.

    Public for the same reason as preprocess_for_template: the accuracy harness
    must measure this exact choice, not a copy of it.
    """
    if text_layer is not None:
        return PageExtraction(
            fields=extract_fields_from_text_layer(template.template_id, template.version, text_layer),
            review_image_bytes=generic.field_image_bytes,
            text_native=True,
        )
    processed = preprocess_for_template(page_bytes, template, generic)
    return PageExtraction(
        fields=extract_fields(template.template_id, template.version, processed.field_image_bytes),
        review_image_bytes=processed.field_image_bytes,
        text_native=False,
    )


def prepare_document(
    buffer: bytes, filename: Optional[str], type_hint: Optional[str]
) -> tuple[list[bytes], IngestionMetadata, list[Optional[TextLayer]]]:
    """
    Module 1 for a whole upload: the pages, the ingestion metadata, and each
    page's text layer (None where a page has none). Raises IngestionError for a
    file that cannot be ingested. Shared by the synchronous endpoint and the
    batch worker so the two cannot ingest differently.
    """
    pages, metadata = ingest_file(buffer, filename=filename, type_hint=type_hint)

    # The text layers are only worth extracting when ingestion found at least
    # one text-native page; everything else keeps the raster path untouched.
    if any(metadata.text_native_pages):
        text_layers = extract_text_layers(buffer)
    else:
        text_layers = [None] * len(pages)
    return pages, metadata, text_layers


def process_page(
    page_bytes: bytes, page_index: int, type_hint: Optional[str], text_layer: Optional[TextLayer] = None
) -> PageResult:
    """
    Modules 2 -> 8 for one page, persisting its result and enqueueing whatever
    Module 9 needs. Public so the batch worker can run it a page at a time and
    record progress between pages.
    """
    # Pass 1 - generic. Module 2 has no idea what this document is yet, which
    # is the whole reason a second pass exists.
    #
    # A text-native page is a PDF render, upright by construction, so deskew is
    # skipped for it entirely: estimating skew on a page that has none can only
    # ever introduce error.
    processed = preprocess_image(page_bytes, deskew=text_layer is None)
    if text_layer is not None:
        log.info("page %d deskew: skipped (text-native)", page_index)
    else:
        log.info(
            "page %d deskew: detected=%+.2f deg method=%s applied=%s",
            page_index, processed.skew_angle_detected, processed.deskew_method, processed.deskew_applied,
        )

    identification = identify_document_type(processed.image_bytes, type_hint)

    if not identification.confirmed:
        queue_id = enqueue_new_template_needed(type_hint, identification.reason)
        return PageResult(page_index=page_index, confirmed=False, reason=identification.reason, review_queue_id=queue_id)

    # get_template_version, not get_latest_template - extract_fields/validate_fields
    # both need the EXACT version Module 5 just confirmed against.
    template = get_template_version(identification.template_id, identification.version)

    # Reads from field_image_bytes, never image_bytes, on the raster path: the
    # binarized rendering that makes anchors detectable destroys the stroke
    # detail character recognition needs (see ProcessedImage).
    extraction = extract_page_fields(template, page_bytes, processed, text_layer)
    summary = validate_fields(template, extraction.fields)

    compiled = compile_output(identification.template_id, identification.version, summary)
    # Store the grayscale rendering, not the binarized one - this is what a
    # human reviewer will look at.
    result_id = persist_result(compiled, page_image=make_review_render(extraction.review_image_bytes))

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
        text_native=extraction.text_native,
    )


@router.post("/documents/process", response_model=ProcessDocumentResponse)
async def process_document(file: UploadFile = File(...), type_hint: Optional[str] = Form(None)) -> ProcessDocumentResponse:
    buffer = await file.read()
    try:
        pages, metadata, text_layers = prepare_document(buffer, file.filename, type_hint)
    except IngestionError as exc:
        raise HTTPException(status_code=422, detail={"reason_code": exc.reason_code, "message": str(exc)})

    page_results = [process_page(page_bytes, i, type_hint, text_layers[i]) for i, page_bytes in enumerate(pages)]

    return ProcessDocumentResponse(filename=metadata.filename, page_count=metadata.page_count, pages=page_results)
