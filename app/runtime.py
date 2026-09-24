"""
Process-level setup shared by everything that runs the pipeline: the API server
and the batch worker. Each is its own process, so each must do this itself.
"""

import logging

import pytesseract

from app.config import TESSERACT_CMD


def configure_tesseract() -> None:
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def configure_logging() -> None:
    # uvicorn configures only its own loggers, so without this every INFO line from
    # app.* - including the per-page deskew angle - is silently dropped.
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
