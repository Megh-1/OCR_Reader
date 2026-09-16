import io

import fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

import app.modules.ingestion as ingestion
from app.main import app

client = TestClient(app)


def make_pdf(num_pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(num_pages):
        doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def make_image(fmt: str = "JPEG", size: tuple[int, int] = (20, 20)) -> bytes:
    img = Image.new("RGB", size, color=(255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_valid_pdf():
    resp = client.post("/ingest", files={"file": ("test.pdf", make_pdf(1), "application/pdf")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["source_mime"] == "application/pdf"
    assert body["metadata"]["page_count"] == 1
    assert len(body["pages"]) == 1


def test_valid_jpg():
    resp = client.post("/ingest", files={"file": ("test.jpg", make_image("JPEG"), "image/jpeg")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["source_mime"] == "image/jpeg"
    assert body["metadata"]["page_count"] == 1
    assert len(body["pages"]) == 1


def test_valid_png():
    resp = client.post("/ingest", files={"file": ("test.png", make_image("PNG"), "image/png")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["source_mime"] == "image/png"
    assert body["metadata"]["page_count"] == 1
    assert len(body["pages"]) == 1


def test_corrupted_image_is_rejected():
    truncated = make_image("PNG")[:20]
    resp = client.post("/ingest", files={"file": ("bad.png", truncated, "image/png")})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "corrupt_file"


def test_extension_spoofed_file_is_rejected():
    data = b"This is plain text pretending to be a PDF via its filename."
    resp = client.post("/ingest", files={"file": ("fake.pdf", data, "application/pdf")})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "unsupported_type"


def test_oversized_file_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ingestion, "MAX_UPLOAD_BYTES", 10)
    data = make_image("PNG")
    resp = client.post("/ingest", files={"file": ("big.png", data, "image/png")})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "oversized"


def test_pdf_exceeding_max_pages_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ingestion, "MAX_PAGES", 2)
    data = make_pdf(3)
    resp = client.post("/ingest", files={"file": ("multi.pdf", data, "application/pdf")})
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "too_many_pages"
