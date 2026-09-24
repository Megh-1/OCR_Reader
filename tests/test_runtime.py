"""Process-level Tesseract setup shared by the API and the batch worker."""

import os

import pytesseract

import app.runtime as runtime
from app.config import TESSERACT_OMP_THREAD_LIMIT


def test_each_tesseract_process_is_capped_to_one_thread_by_default(monkeypatch):
    """Without the cap a Linux worker pool thrashes on OpenMP spin-waits (186 s a document, measured)."""
    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)

    runtime.configure_tesseract()

    assert os.environ["OMP_THREAD_LIMIT"] == str(TESSERACT_OMP_THREAD_LIMIT) == "1"


def test_an_operators_own_thread_limit_wins(monkeypatch):
    monkeypatch.setenv("OMP_THREAD_LIMIT", "4")

    runtime.configure_tesseract()

    assert os.environ["OMP_THREAD_LIMIT"] == "4"


def test_the_binary_is_pointed_at_when_one_is_resolved(monkeypatch):
    monkeypatch.setattr(runtime, "TESSERACT_CMD", "/usr/bin/tesseract")
    monkeypatch.setattr(pytesseract.pytesseract, "tesseract_cmd", "tesseract")

    runtime.configure_tesseract()

    assert pytesseract.pytesseract.tesseract_cmd == "/usr/bin/tesseract"


def test_tesseract_children_inherit_the_cap(monkeypatch):
    """The cap only works because the OCR runs in child processes that inherit the environment."""
    import subprocess
    import sys

    monkeypatch.delenv("OMP_THREAD_LIMIT", raising=False)
    runtime.configure_tesseract()

    out = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('OMP_THREAD_LIMIT'))"], capture_output=True, text=True
    ).stdout.strip()

    assert out == "1"
