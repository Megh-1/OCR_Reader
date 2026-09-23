import logging

import pytesseract
from fastapi import FastAPI

from app.config import TESSERACT_CMD
from app.modules.ingestion import router as ingestion_router
from app.modules.output_compiler import router as output_router
from app.modules.pipeline import router as pipeline_router
from app.modules.review_queue import router as review_router
from app.modules.template_store import router as template_router

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

# uvicorn configures only its own loggers, so without this every INFO line from
# app.* - including the per-page deskew angle - is silently dropped.
_app_logger = logging.getLogger("app")
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
    _app_logger.addHandler(_handler)
_app_logger.setLevel(logging.INFO)

app = FastAPI(title="IDP Pipeline")

app.include_router(ingestion_router)
app.include_router(template_router)
app.include_router(output_router)
app.include_router(review_router)
app.include_router(pipeline_router)
