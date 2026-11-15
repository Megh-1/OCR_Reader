"""Page-first mode through process_page: page engines read a page no template matched, and it is never refused for that reason."""

import io
from types import SimpleNamespace

import pytest
from PIL import Image

import app.modules.pipeline as pipeline
from app import config
from app.modules import page_pipeline
from app.modules.grounding import PageText
from app.modules.page_reader import FIELD_IDS, PageRead, ReadField
from app.modules.vlm_reader.engines import EngineSpec
from scripts.seed_templates import AIR_WAYBILL, seed

GOOD_AWB = "176-06067736"
SPECS = [EngineSpec(name=n, kind="openai_compatible", role="page", url="http://x.invalid/v1", model="m") for n in ("page_a", "page_b")]


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    seed([AIR_WAYBILL])


@pytest.fixture()
def page_mode(monkeypatch):
    monkeypatch.setattr(config, "READER_MODE", "page_first")
    monkeypatch.setattr(config, "READER_PAGE_ENGINES", ["page_a", "page_b"])
    monkeypatch.setattr(page_pipeline, "configured_specs", lambda: SPECS)
    monkeypatch.setattr(pipeline, "identify_document_type", lambda *a, **k: SimpleNamespace(confirmed=False, reason="no_match", template_id=None, version=None, alignment=None))
    monkeypatch.setattr(page_pipeline, "page_text", lambda *a, **k: PageText(layer="", ocr=f"waybill {GOOD_AWB} emirates"))


def _page() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (800, 1100), "white").save(buf, "PNG")
    return buf.getvalue()


def _read(engine: str, values: dict[str, str], ok: bool = True) -> PageRead:
    fields = {fid: ReadField(value=values.get(fid, ""), source_text=values.get(fid, "")) for fid in FIELD_IDS}
    return PageRead(fields=fields, ok=ok, reason=None if ok else "reader_failed", engine=engine)


def test_an_unmatched_page_is_read_by_the_page_engines_and_stored_with_evidence(page_mode, monkeypatch):
    monkeypatch.setattr(page_pipeline, "read_pages", lambda specs, b, t: [_read("page_a", {"awb_number": GOOD_AWB}), _read("page_b", {"awb_number": GOOD_AWB})])
    page = pipeline.process_page(_page(), 0, None)
    assert page.confirmed and page.reader == "page" and page.result.template_matched is False and page.result.template_id == "air_waybill"
    f = page.result.fields["awb_number"]
    assert f.approval_tier == "auto" and f.engines == ["page_a", "page_b"] and f.grounded is True and f.agreed is True and f.source_text == GOOD_AWB
    assert page.result.fields["shipper_name"].approval_tier != "auto"  # blank
    assert page.review_queue_id is None


def test_one_engine_down_the_others_still_read_the_page_but_nothing_is_auto(page_mode, monkeypatch):
    monkeypatch.setattr(page_pipeline, "read_pages", lambda specs, b, t: [_read("page_a", {"awb_number": GOOD_AWB}), _read("page_b", {}, ok=False)])
    page = pipeline.process_page(_page(), 0, None)
    assert page.confirmed and page.reader == "page"
    assert all(f.approval_tier != "auto" for f in page.result.fields.values())
    assert "single_reader" in page.result.fields["awb_number"].reasons


def test_disagreement_is_a_field_review(page_mode, monkeypatch):
    monkeypatch.setattr(page_pipeline, "read_pages", lambda specs, b, t: [_read("page_a", {"awb_number": GOOD_AWB}), _read("page_b", {"awb_number": "176-06067744"})])
    page = pipeline.process_page(_page(), 0, None)
    assert page.result.fields["awb_number"].approval_tier == "review" and page.result.fields["awb_number"].needs_review
    assert page.field_review_queue_ids and page.result.overall_status == "needs_review"


def test_every_engine_down_on_an_unmatched_page_is_refused_as_before(page_mode, monkeypatch):
    monkeypatch.setattr(page_pipeline, "read_pages", lambda specs, b, t: [_read("page_a", {}, ok=False), _read("page_b", {}, ok=False)])
    page = pipeline.process_page(_page(), 0, None)
    assert not page.confirmed and page.reason == "no_match"


def test_page_mode_is_off_without_engines(monkeypatch):
    monkeypatch.setattr(config, "READER_PAGE_ENGINES", [])
    assert not page_pipeline.enabled()
    monkeypatch.setattr(config, "READER_PAGE_ENGINES", ["page_a"])
    monkeypatch.setattr(config, "READER_MODE", "template")
    assert not page_pipeline.enabled()


def test_the_export_reports_template_id_null_for_an_unmatched_page(page_mode, monkeypatch, client):
    monkeypatch.setattr(page_pipeline, "read_pages", lambda specs, b, t: [_read("page_a", {"awb_number": GOOD_AWB}), _read("page_b", {"awb_number": GOOD_AWB})])
    page = pipeline.process_page(_page(), 0, None)
    body = client.get(f"/results/{page.extraction_result_id}").json()
    assert body["template_matched"] is False
