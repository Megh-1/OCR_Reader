from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class IngestionMetadata(BaseModel):
    filename: str
    upload_timestamp: datetime
    type_hint: Optional[str] = None
    page_count: int
    source_mime: str
