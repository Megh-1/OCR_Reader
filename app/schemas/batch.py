from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

DocumentStatus = Literal["queued", "processing", "retrying", "completed", "failed"]
BatchStatus = Literal["queued", "processing", "completed", "completed_with_errors", "failed"]


class BatchDocumentSubmitted(BaseModel):
    index: int
    filename: str
    status: DocumentStatus


class BatchSubmitResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    document_count: int
    documents: list[BatchDocumentSubmitted]
    status_url: str


class BatchDocumentStatus(BaseModel):
    index: int
    filename: str
    status: DocumentStatus
    attempts: int  # times a worker has started this document; 2 means it was retried once
    error: Optional[str] = None  # the last failure, kept while retrying and after giving up
    pages_done: int = 0
    page_count: Optional[int] = None
    # The same body POST /documents/process returns, present only once the document is completed.
    result: Optional[dict] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class BatchStatusResponse(BaseModel):
    batch_id: str
    status: BatchStatus
    created_at: datetime
    type_hint: Optional[str] = None
    counts: dict[str, int]  # documents per DocumentStatus, every status present
    documents: list[BatchDocumentStatus]
