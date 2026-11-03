"""
End-to-end orchestrator: chains Modules 1 -> 2 -> 5 -> 6 -> 7 -> 8 via their
existing, already-tested public functions only. Nothing here reimplements
any module's own logic - this file's only job is the wiring between them,
plus routing both of Module 9's entry points exactly as the reference
doc's architecture diagram describes:
  - Module 5 rejects (any reason) -> enqueue_new_template_needed
  - Module 7/8 flags a field       -> one enqueue_field_review call per field
  - Module 7 accepts a field but gives it approval_tier spot_check -> one enqueue_spot_check
    call per field, so the human-in-the-loop sees the "probably right" tier too
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image
from pydantic import BaseModel

from app.models.text_layer import TextLayer
from app.modules.alignment import Alignment, align_page
from app.modules.field_extraction import FieldExtractionResult, extract_fields, extract_fields_from_text_layer
from app.modules.ingestion import IngestionError, IngestionMetadata, extract_text_layers, ingest_file
from app.modules.output_compiler import compile_output, make_review_render, persist_result
from app.modules.preprocessing import ProcessedImage, preprocess_image
from app.modules import trust
from app.modules.vlm_reader import reader as vlm
from app.modules.vlm_reader.client import ReaderUnavailable
from app.modules.review_queue import enqueue_field_review, enqueue_new_template_needed, enqueue_spot_check
from app.modules.template_store import get_template_version
from app.modules.type_identification import identify_document_type
from app.modules.validation import demote_auto, has_validator, validate_fields
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
    spot_check_queue_ids: list[int] = []  # one id per accepted field that is spot_check rather than auto
    text_native: bool = False  # True when fields were read from the PDF's embedded text, with no OCR
    # Who read the page's fields: "text_layer", "vlm" (the vision-model reader), "ocr" (Tesseract), or "ocr_fallback" (the
    # template asks for the vision reader and it could not be used for this page, so Tesseract read it).
    reader: str = "ocr"
    # How the template's boxes were moved to fit this page (landmarks matched, scale, shift, rms);
    # None when they were read exactly where the template stored them.
    alignment: Optional[dict] = None


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
    alignment: Optional[Alignment] = None  # the boxes were moved by this; None = read at the stored positions
    reader: str = "ocr"  # "text_layer" | "vlm" | "ocr" | "ocr_fallback"


def _image_size(image_bytes: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(image_bytes)) as im:
        return im.size


def extract_page_fields(
    template: TemplateOut,
    page_bytes: bytes,
    generic: ProcessedImage,
    text_layer: Optional[TextLayer],
    alignment: Optional[Alignment] = None,
) -> PageExtraction:
    """
    Module 6 for one page, choosing its path.

    `alignment` is the one Module 5 verified the page with. Boxes are read where it puts them, so
    the boxes that confirmed the document are the boxes that read it. Pass 2 can change the page's
    frame (a photographed page warped to the template's aspect ratio): then the alignment found
    in pass 1 no longer describes the pixels, so it is fitted again on the pass-2 page. If that
    fails the page is read at the stored positions and Module 7 is left to flag what it cannot
    validate - logged, since it means the two passes disagreed about the page.

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
        text_fields = extract_fields_from_text_layer(template.template_id, template.version, text_layer, alignment)
        # The text layer is exact, but a template's boxes read it at fixed positions: a page whose typed values sit off their cells
        # (another print of the form, a form fed through the printer off-centre) reads a neighbour's words, and the alignment cannot
        # see it because it is fitted on the printed labels. Module 7 rejecting anything - a format, a check digit, a cross-field
        # rule, a blank - is the evidence that the boxes missed, so such a page goes to the vision reader, with the layer's words as
        # its second reader. Measured on 24 hard digital pages of the two forms: 3-26 rejected fields each, every wrong value Module 7
        # accepted was a spot check. A clean page stays on the exact read, and a reader that is unavailable leaves the page as it was.
        if vlm.enabled(template) and validate_fields(template, text_fields).flagged:
            try:
                page = vlm.read_page(template, page_bytes, generic, text_layer=text_layer, verified=alignment)
                return PageExtraction(fields=page.fields, review_image_bytes=generic.field_image_bytes, text_native=False, alignment=page.alignment, reader="vlm")
            except ReaderUnavailable as exc:
                log.warning("template %s: the text layer read was doubtful but the vision reader could not be used (%s); keeping the text layer", template.template_id, exc)
        return PageExtraction(
            fields=text_fields,
            review_image_bytes=generic.field_image_bytes,
            text_native=True,
            alignment=alignment,
            reader="text_layer",
        )
    reader = "ocr"
    if vlm.enabled(template):
        # A scanned or photographed page of a template that names a reader profile goes to the vision model. Anything that stops it
        # (service down, profile missing, page not aligned) is not an error: the page is read the ordinary way, and says so.
        try:
            page = vlm.read_page(template, page_bytes, generic, verified=alignment)
            return PageExtraction(fields=page.fields, review_image_bytes=generic.field_image_bytes, text_native=False, alignment=page.alignment, reader="vlm")
        except ReaderUnavailable as exc:
            log.warning("template %s: the vision reader could not be used (%s); reading the page with Tesseract", template.template_id, exc)
            reader = "ocr_fallback"
    processed = preprocess_for_template(page_bytes, template, generic)
    if alignment is not None and _image_size(processed.image_bytes) != _image_size(generic.image_bytes):
        with Image.open(io.BytesIO(processed.image_bytes)) as pass2_page:
            pass2_page.load()
            alignment = align_page(pass2_page, template.landmarks)
        if alignment is None:
            log.warning("template %s: alignment found in pass 1 could not be refitted on the pass-2 page", template.template_id)
    return PageExtraction(
        fields=extract_fields(template.template_id, template.version, processed.field_image_bytes, alignment),
        review_image_bytes=processed.field_image_bytes,
        text_native=False,
        alignment=alignment,
        reader=reader,
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
    extraction = extract_page_fields(template, page_bytes, processed, text_layer, identification.alignment)
    if extraction.alignment is not None:
        log.info("page %d aligned: %s", page_index, extraction.alignment.summary())
    summary = validate_fields(template, extraction.fields)
    if extraction.reader == "ocr_fallback":
        # The template asks for the vision reader and could not get it: whatever Tesseract read here is at most a spot check.
        demoted = demote_auto(summary)
        log.info("page %d: read by Tesseract because the vision reader could not be used; %d auto value(s) capped at spot_check", page_index, demoted)

    compiled = compile_output(identification.template_id, identification.version, summary)

    # Audited auto-approval: a spot check on a field whose values a person has checked enough (trust.py) is approved
    # as auto, bar a random few that still go to a person. Decided BEFORE the result is stored so the stored tier is
    # the final one, and recorded in auto_basis so it is never mistaken for a Module 7 auto.
    template_fields = {f.id: f for f in template.fields}

    def spot_why(field_id: str, accepted) -> str:
        return (
            "blank_optional" if template_fields[field_id].optional and not accepted.value.strip()
            else "below_auto_threshold" if has_validator(template_fields[field_id]) else "no_validator"
        )

    spot_checks = {fid: acc for fid, acc in summary.accepted.items() if acc.approval_tier == "spot_check"}
    # Audited auto-approval counts what people found when they checked a field, whoever read it. A vision-read value is a different
    # reader with a different error profile, so its spot checks stay spot checks until evidence has been gathered on ITS reads.
    trusted = trust.trusted_fields(identification.template_id, identification.version) if spot_checks and extraction.reader not in ("vlm", "ocr_fallback") else frozenset()
    audited: set[str] = set()
    for field_id, accepted in spot_checks.items():
        if field_id in trusted and spot_why(field_id, accepted) in trust.TRUSTABLE_WHY and accepted.value.strip():
            if trust.audit_this():
                audited.add(field_id)
            else:
                compiled.fields[field_id].approval_tier = "auto"
                compiled.fields[field_id].auto_basis = "trusted_field"

    # Store the grayscale rendering, not the binarized one - this is what a
    # human reviewer will look at.
    result_id = persist_result(compiled, page_image=make_review_render(extraction.review_image_bytes))

    field_review_ids = [
        enqueue_field_review(result_id, field_id, flagged.value, flagged.reasons, flagged.confidence)
        for field_id, flagged in summary.flagged.items()
    ]
    spot_check_ids = [
        enqueue_spot_check(result_id, field_id, accepted.value, accepted.confidence, spot_why(field_id, accepted), audit=field_id in audited)
        for field_id, accepted in spot_checks.items()
        if compiled.fields[field_id].approval_tier == "spot_check"
    ]

    return PageResult(
        page_index=page_index,
        confirmed=True,
        extraction_result_id=result_id,
        result=compiled,
        field_review_queue_ids=field_review_ids,
        spot_check_queue_ids=spot_check_ids,
        text_native=extraction.text_native,
        reader=extraction.reader,
        alignment=extraction.alignment.summary() if extraction.alignment is not None else None,
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
