"""
Process-level setup shared by everything that runs the pipeline: the API server
and the batch worker. Each is its own process, so each must do this itself.
"""

import logging
import os

import pytesseract

from app.config import TESSERACT_CMD, TESSERACT_OMP_THREAD_LIMIT


def configure_tesseract() -> None:
    """
    Point pytesseract at the binary and cap each Tesseract process's OpenMP threads.

    The cap is an environment variable because every read is a child process that
    inherits it; setdefault, so an operator's own OMP_THREAD_LIMIT wins. Without it a
    Linux worker pool thrashes - see TESSERACT_OMP_THREAD_LIMIT for the measurements.
    """
    if TESSERACT_CMD:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD
    os.environ.setdefault("OMP_THREAD_LIMIT", str(TESSERACT_OMP_THREAD_LIMIT))


def configure_logging() -> None:
    # uvicorn configures only its own loggers, so without this every INFO line from
    # app.* - including the per-page deskew angle - is silently dropped.
    app_logger = logging.getLogger("app")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s - %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(logging.INFO)
