"""
Text-native fast path: a PDF page with a real embedded text layer is read
straight from that text, with no OCR in Module 6. Anything else - a scan, an
OCR'd scan, a photo - must fall through to the raster + Tesseract path
unchanged.
"""

import io
from unittest.mock import Mock

import fitz
import numpy as np
import pytest
import pytesseract
from PIL import Image

import app.modules.field_extraction as field_extraction
import app.modules.type_identification as type_identification
from app.modules.field_extraction import extract_field_from_text_layer, extract_fields_from_text_layer
from app.modules.ingestion import extract_text_layers, ingest_file
from app.modules.validation import validate_field
from app.models.text_layer import TextLayer, TextWord
from app.schemas.template import TemplateField

PAGE_W, PAGE_H = 612, 792


# --- builders ----------------------------------------------------------------


def _png(color=220) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (PAGE_W, PAGE_H), color=(color, color, color)).save(buf, format="PNG")
    return buf.getvalue()


def text_pdf(lines, rotation=0) -> bytes:
    """A digital-born page: each (text, x, y) is drawn as real, visible text."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    for text, x, y in lines:
        page.insert_text((x, y), text, fontsize=11)
    if rotation:
        page.set_rotation(rotation)
    data = doc.tobytes()
    doc.close()
    return data


def scan_pdf(overlay=None) -> bytes:
    """A scanned page: a raster image, plus - when `overlay` is given - the
    INVISIBLE recognised text that OCR software lays over it (render mode 3)."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_image(page.rect, stream=_png())
    if overlay:
        page.insert_text((72, 120), overlay, fontsize=11, render_mode=3)
    data = doc.tobytes()
    doc.close()
    return data


def box_for(pdf_bytes: bytes, phrase: str, pad_x=3.0, pad_y=2.5):
    """A fractional field bbox tight around `phrase`, the way a template built
    from the document's own geometry would draw it."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    rect = doc[0].search_for(phrase)[0]
    doc.close()
    return [
        (rect.x0 - pad_x) / PAGE_W,
        (rect.y0 - pad_y) / PAGE_H,
        (rect.x1 + pad_x) / PAGE_W,
        (rect.y1 + pad_y) / PAGE_H,
    ]


def field(field_id, bbox, regex=None, floor=0.75) -> TemplateField:
    return TemplateField(
        id=field_id, label=field_id, bbox=tuple(bbox), data_type="string", validation_regex=regex, confidence_floor=floor
    )


def save_template(client, template_id, fields):
    resp = client.post(
        "/templates",
        json={
            "template_id": template_id,
            "anchors": [{"text": "HEADER", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85}],
            "fields": [
                {
                    "id": f.id, "label": f.label, "bbox": list(f.bbox), "data_type": f.data_type,
                    "validation_regex": f.validation_regex, "confidence_floor": f.confidence_floor,
                }
                for f in fields
            ],
        },
    )
    assert resp.status_code == 201
    return resp.json()["version"]


@pytest.fixture()
def no_tesseract(monkeypatch):
    """Fails the test on ANY Tesseract launch, at the lowest level: every
    pytesseract API funnels through run_tesseract, so patching only the
    wrappers would miss a call that reaches the subprocess another way."""
    launches = []

    def forbidden(*args, **kwargs):
        launches.append((args, kwargs))
        raise AssertionError("Tesseract was launched on the text-native path")

    monkeypatch.setattr(pytesseract.pytesseract, "run_tesseract", forbidden)
    for name in ("image_to_data", "image_to_string", "image_to_osd"):
        monkeypatch.setattr(pytesseract, name, Mock(side_effect=AssertionError(f"pytesseract.{name} was called")))
    return launches


# --- the requested guarantee: zero pytesseract calls -------------------------


def test_text_native_fields_extract_with_zero_tesseract_calls(client, unique_doc_type, no_tesseract):
    pdf = text_pdf([("ACME EXPORTS LTD", 72, 120), ("INV-2025-0042", 72, 200), ("13752.00", 350, 300)])
    fields = [
        field("vendor", box_for(pdf, "ACME EXPORTS LTD")),
        field("invoice_no", box_for(pdf, "INV-2025-0042")),
        field("total", box_for(pdf, "13752.00")),
    ]
    version = save_template(client, unique_doc_type, fields)
    layer = extract_text_layers(pdf)[0]
    assert layer is not None

    results = extract_fields_from_text_layer(unique_doc_type, version, layer)

    assert {k: v.raw_text for k, v in results.items()} == {
        "vendor": "ACME EXPORTS LTD",
        "invoice_no": "INV-2025-0042",
        "total": "13752.00",
    }
    assert all(r.source == "text_layer" and r.psm_used == 0 for r in results.values())
    assert all(r.aggregate_confidence == 100.0 for r in results.values())
    assert no_tesseract == []  # not one subprocess launch
    pytesseract.image_to_data.assert_not_called()


# --- geometry: what "overlaps the field" means -------------------------------


def test_multi_word_multi_line_field_reads_in_reading_order():
    pdf = text_pdf([("SURVEY NO.13/1", 72, 100), ("KANEKAL VILLAGE", 72, 112), ("elsewhere", 72, 400)])
    layer = extract_text_layers(pdf)[0]
    both_lines = [(72 - 3) / PAGE_W, (100 - 14) / PAGE_H, (72 + 100) / PAGE_W, (112 + 6) / PAGE_H]

    result = extract_field_from_text_layer(layer, field("address", both_lines))

    assert result.raw_text == "SURVEY NO.13/1 KANEKAL VILLAGE"


def test_a_neighbouring_line_grazing_the_box_is_not_pulled_in():
    # Line spacing (12pt) is tighter than a word box is tall, so a box drawn
    # tight to line one still intersects the words of line two. Any-overlap
    # would pull the whole neighbouring line into the value.
    pdf = text_pdf([("FIRST LINE", 72, 100), ("SECOND LINE", 72, 112)])
    layer = extract_text_layers(pdf)[0]
    box = box_for(pdf, "FIRST LINE")

    neighbour = next(w for w in layer.words if w.text == "SECOND")
    assert neighbour.bbox[1] < box[3], "premise: the box does graze the second line"

    assert extract_field_from_text_layer(layer, field("first", box)).raw_text == "FIRST LINE"


def test_a_word_is_taken_when_at_least_half_of_it_is_inside_the_box():
    def layer_of(*words):
        return TextLayer(words=tuple(TextWord(t, b) for t, b in words))

    box = (0.14, 0.10, 0.40, 0.20)
    layer = layer_of(
        ("MOSTLY_IN", (0.10, 0.10, 0.20, 0.20)),   # 60% of its width inside
        ("MOSTLY_OUT", (0.10, 0.30, 0.20, 0.40)),  # nowhere near
        ("EDGE_OUT", (0.05, 0.10, 0.15, 0.20)),    # 10% inside
    )

    assert extract_field_from_text_layer(layer, field("f", box)).raw_text == "MOSTLY_IN"


def test_an_empty_box_is_flagged_rather_than_accepted():
    layer = TextLayer(words=(TextWord("SOMEWHERE", (0.1, 0.1, 0.2, 0.12)),))
    f = field("f", (0.6, 0.6, 0.9, 0.7), floor=0.75)

    result = extract_field_from_text_layer(layer, f)

    assert result.raw_text == "" and result.aggregate_confidence == 0.0
    verdict = validate_field(f, result)
    assert verdict.accepted is False and "low_confidence" in verdict.reasons


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_word_fractions_land_where_the_page_is_actually_drawn(rotation):
    """get_text() coordinates are in the UNROTATED page space, but the page is
    rendered - and templates are drawn - in its displayed orientation. Checked
    against the rendered pixels rather than against the arithmetic under test."""
    pdf = text_pdf([("TOPLEFT", 72, 100)], rotation=rotation)

    doc = fitz.open(stream=pdf, filetype="pdf")
    pix = doc[0].get_pixmap(dpi=72)
    ink = np.array(Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")) < 128
    ys, xs = np.where(ink)
    ink_centre = ((xs.min() + xs.max()) / 2 / pix.width, (ys.min() + ys.max()) / 2 / pix.height)
    raw = fitz.Rect(doc[0].get_text("words")[0][:4])
    raw_centre = (
        (raw.x0 + raw.x1) / 2 / doc[0].rect.width,
        (raw.y0 + raw.y1) / 2 / doc[0].rect.height,
    )
    doc.close()

    word = extract_text_layers(pdf)[0].words[0]
    centre = ((word.bbox[0] + word.bbox[2]) / 2, (word.bbox[1] + word.bbox[3]) / 2)

    assert abs(centre[0] - ink_centre[0]) < 0.02 and abs(centre[1] - ink_centre[1]) < 0.02
    if rotation:
        # Non-vacuous: without the rotation transform this would be far off.
        assert max(abs(raw_centre[0] - ink_centre[0]), abs(raw_centre[1] - ink_centre[1])) > 0.4


# --- what counts as text-native ---------------------------------------------


def test_a_pdf_with_no_text_layer_is_not_text_native():
    pdf = scan_pdf()
    assert extract_text_layers(pdf) == [None]
    _, metadata = ingest_file(pdf, "scan.pdf", None)
    assert metadata.text_native_pages == [False]


def test_an_ocr_overlay_is_not_mistaken_for_native_text():
    # The premise that makes the guard necessary: get_text() DOES return the
    # overlay's words. Read as native they would carry full confidence.
    pdf = scan_pdf(overlay="RECOGNISED BY OCR")
    doc = fitz.open(stream=pdf, filetype="pdf")
    assert [w[4] for w in doc[0].get_text("words")] == ["RECOGNISED", "BY", "OCR"]
    doc.close()

    assert extract_text_layers(pdf) == [None]
    _, metadata = ingest_file(pdf, "searchable-scan.pdf", None)
    assert metadata.text_native_pages == [False]


def test_native_flag_is_per_page():
    doc = fitz.open()
    doc.new_page(width=PAGE_W, height=PAGE_H).insert_text((72, 100), "DIGITAL PAGE", fontsize=11)
    scanned = doc.new_page(width=PAGE_W, height=PAGE_H)
    scanned.insert_image(scanned.rect, stream=_png())
    pdf = doc.tobytes()
    doc.close()

    _, metadata = ingest_file(pdf, "mixed.pdf", None)

    assert metadata.page_count == 2
    assert metadata.text_native_pages == [True, False]


def test_images_are_never_text_native():
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), color=255).save(buf, format="JPEG")
    _, metadata = ingest_file(buf.getvalue(), "photo.jpg", None)
    assert metadata.text_native_pages == [False]


# --- the pipeline ------------------------------------------------------------


def _post_pdf(client, pdf: bytes, type_hint: str):
    return client.post(
        "/documents/process",
        files={"file": ("doc.pdf", pdf, "application/pdf")},
        data={"type_hint": type_hint},
    )


def test_pipeline_reads_a_text_native_pdf_without_ocr_in_module_6(client, unique_doc_type, monkeypatch):
    pdf = text_pdf([("ACME EXPORTS LTD", 72, 120), ("13752.00", 350, 300)])
    save_template(
        client,
        unique_doc_type,
        [
            field("vendor", box_for(pdf, "ACME EXPORTS LTD")),
            field("total", box_for(pdf, "13752.00"), regex=r"^\d+\.\d{2}$"),
        ],
    )
    # Module 5 still verifies anchors on the raster, so its OCR entry point is
    # mocked. Module 6's is made a tripwire: reaching it fails the test.
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    monkeypatch.setattr(
        field_extraction.pytesseract, "image_to_data", Mock(side_effect=AssertionError("Module 6 ran OCR"))
    )

    resp = _post_pdf(client, pdf, unique_doc_type)

    assert resp.status_code == 200
    page = resp.json()["pages"][0]
    assert page["confirmed"] is True
    assert page["text_native"] is True
    assert {k: v["value"] for k, v in page["result"]["fields"].items()} == {
        "vendor": "ACME EXPORTS LTD",
        "total": "13752.00",
    }
    assert page["result"]["overall_status"] == "complete"
    # A reviewer still gets the real page image for this result.
    assert client.get(f"/results/{page['extraction_result_id']}/image").status_code == 200


@pytest.mark.parametrize("pdf_factory", [scan_pdf, lambda: scan_pdf(overlay="RECOGNISED BY OCR")], ids=["image-only", "ocr-overlay"])
def test_pipeline_falls_through_to_ocr_when_there_is_no_native_text(client, unique_doc_type, monkeypatch, pdf_factory):
    save_template(client, unique_doc_type, [field("vendor", [0.1, 0.1, 0.5, 0.2])])
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="HEADER"))
    ocr = Mock(return_value={"text": ["FROM", "OCR"], "conf": [88, 90]})
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", ocr)

    resp = _post_pdf(client, pdf_factory(), unique_doc_type)

    assert resp.status_code == 200
    page = resp.json()["pages"][0]
    assert page["text_native"] is False
    assert ocr.call_count >= 1  # the raster + Tesseract path ran, unchanged
    assert page["result"]["fields"]["vendor"]["value"] == "FROM OCR"
