from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class IngestionMetadata(BaseModel):
    filename: str
    upload_timestamp: datetime
    type_hint: Optional[str] = None
    page_count: int
    source_mime: str
    # One flag per page: True when the page carries a real embedded text layer
    # (see extract_text_layers), so Module 6 can read it without OCR.
    text_native_pages: list[bool] = []
