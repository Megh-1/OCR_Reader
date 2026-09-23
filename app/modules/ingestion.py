import base64
import io
from datetime import datetime, timezone
from typing import Optional

import fitz
import magic
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from PIL import Image

from app.config import ALLOWED_MIME_TYPES, MAX_PAGES, MAX_UPLOAD_BYTES, PDF_RENDER_DPI
from app.models.ingestion import IngestionMetadata
from app.models.text_layer import TextLayer, TextWord
from app.utils.geometry import pixels_to_relative_bbox

router = APIRouter()


class IngestionError(Exception):
    def __init__(self, reason_code: str, message: str):
        self.reason_code = reason_code
        super().__init__(message)


def validate_size(buffer: bytes) -> None:
    if len(buffer) > MAX_UPLOAD_BYTES:
        raise IngestionError("oversized", f"File exceeds max size of {MAX_UPLOAD_BYTES} bytes")


def detect_mime_type(buffer: bytes) -> str:
    mime_type = magic.from_buffer(buffer, mime=True)
    if mime_type not in ALLOWED_MIME_TYPES:
        raise IngestionError("unsupported_type", f"Detected MIME type '{mime_type}' is not supported")
    return mime_type


def render_pdf_pages(buffer: bytes) -> list[bytes]:
    try:
        doc = fitz.open(stream=buffer, filetype="pdf")
    except Exception as exc:
        raise IngestionError("corrupt_file", f"Could not open PDF: {exc}") from exc

    try:
        if doc.page_count > MAX_PAGES:
            raise IngestionError("too_many_pages", f"PDF has {doc.page_count} pages, max is {MAX_PAGES}")

        try:
            pages = [page.get_pixmap(dpi=PDF_RENDER_DPI).tobytes("png") for page in doc]
        except Exception as exc:
            raise IngestionError("corrupt_file", f"Could not render PDF page: {exc}") from exc
    finally:
        doc.close()

    if not pages:
        raise IngestionError("corrupt_file", "PDF contains no pages")

    return pages


def _has_visible_text(page: "fitz.Page") -> bool:
    """True when at least one text span on the page is actually drawn.

    PyMuPDF's get_text("words") returns invisible text too, and invisible text
    is exactly how OCR software makes a scan searchable: it lays recognised
    characters over the page image in render mode 3. Measured: a raster with
    an OCR overlay yields the overlay's words from get_text() while every span
    in get_texttrace() is type 3 (invisible). Treating that as native text
    would read recognition output as if it were the document's own characters,
    at full confidence, and bypass the confidence gate that exists to catch
    exactly that.
    """
    return any(span["type"] != 3 for span in page.get_texttrace())


def _page_text_layer(page: "fitz.Page") -> Optional[TextLayer]:
    """The page's embedded text as fractional-coordinate words, or None when
    it has none worth trusting (no text at all, or only an invisible OCR
    overlay) and the raster + Tesseract path must be used instead."""
    words = page.get_text("words")
    if not words or not _has_visible_text(page):
        return None

    rect = page.rect
    matrix = page.rotation_matrix
    layer_words = []
    for x0, y0, x1, y1, text, *_ in words:
        if not text.strip():
            continue
        # get_text() reports coordinates in the UNROTATED page space, while the
        # page is rendered (and templates are drawn) in its displayed
        # orientation. Measured on a page rotated 90/180/270: skipping this
        # puts a word 0.68-0.76 of the page away from where it is drawn.
        box = fitz.Rect(x0, y0, x1, y1) * matrix
        box.normalize()
        layer_words.append(
            TextWord(
                text=text,
                # Points are just another linear unit here: the same
                # percent-of-page conversion the pixel paths use.
                bbox=pixels_to_relative_bbox(
                    (box.x0 - rect.x0, box.y0 - rect.y0, box.x1 - rect.x0, box.y1 - rect.y0),
                    rect.width,
                    rect.height,
                ),
            )
        )
    return TextLayer(words=tuple(layer_words)) if layer_words else None


def extract_text_layers(buffer: bytes) -> list[Optional[TextLayer]]:
    """One entry per page of a PDF: its text layer, or None for a page that
    must go through OCR.

    This is an optimisation, so it must never be able to make the pipeline
    fail where the OCR path would have succeeded - a page whose text layer
    cannot be read simply falls through to OCR."""
    doc = fitz.open(stream=buffer, filetype="pdf")
    try:
        layers: list[Optional[TextLayer]] = []
        for page in doc:
            try:
                layers.append(_page_text_layer(page))
            except Exception:
                layers.append(None)
        return layers
    finally:
        doc.close()


def normalize_image(buffer: bytes) -> list[bytes]:
    try:
        img = Image.open(io.BytesIO(buffer))
        img.verify()
    except Exception as exc:
        raise IngestionError("corrupt_file", f"Image failed verification: {exc}") from exc

    try:
        img = Image.open(io.BytesIO(buffer))
        img.load()
        out = io.BytesIO()
        img.convert("RGB").save(out, format="PNG")
    except Exception as exc:
        raise IngestionError("corrupt_file", f"Could not decode image: {exc}") from exc

    return [out.getvalue()]


def ingest_file(
    buffer: bytes, filename: str, type_hint: Optional[str]
) -> tuple[list[bytes], IngestionMetadata]:
    validate_size(buffer)
    mime_type = detect_mime_type(buffer)

    if mime_type == "application/pdf":
        pages = render_pdf_pages(buffer)
        text_native_pages = [layer is not None for layer in extract_text_layers(buffer)]
    else:
        pages = normalize_image(buffer)
        text_native_pages = [False] * len(pages)

    metadata = IngestionMetadata(
        filename=filename,
        upload_timestamp=datetime.now(timezone.utc),
        type_hint=type_hint,
        page_count=len(pages),
        source_mime=mime_type,
        text_native_pages=text_native_pages,
    )
    return pages, metadata


@router.post("/ingest")
async def ingest(file: UploadFile = File(...), type_hint: Optional[str] = Form(None)):
    buffer = await file.read()
    try:
        pages, metadata = ingest_file(buffer, filename=file.filename, type_hint=type_hint)
    except IngestionError as exc:
        raise HTTPException(
            status_code=422,
            detail={"reason_code": exc.reason_code, "message": str(exc)},
        )

    return {
        "metadata": metadata.model_dump(mode="json"),
        "pages": [base64.b64encode(page).decode("ascii") for page in pages],
    }
