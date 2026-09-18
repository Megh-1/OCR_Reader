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
    else:
        pages = normalize_image(buffer)

    metadata = IngestionMetadata(
        filename=filename,
        upload_timestamp=datetime.now(timezone.utc),
        type_hint=type_hint,
        page_count=len(pages),
        source_mime=mime_type,
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
