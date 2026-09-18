import io
from unittest.mock import Mock

from PIL import Image

import app.config as config
import app.modules.field_extraction as field_extraction
from app.schemas.template import TemplateField


def blank_image_bytes(size: tuple[int, int] = (500, 500)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=255).save(buf, format="PNG")
    return buf.getvalue()


def make_field(bbox, data_type="string", field_id="f1") -> TemplateField:
    return TemplateField(
        id=field_id, label="Test", bbox=bbox, data_type=data_type, validation_regex=None, confidence_floor=0.75
    )


def test_normal_field_crop_coordinates_are_exact(monkeypatch):
    image = Image.new("RGB", (1000, 800), color=255)
    image.crop = Mock(wraps=image.crop)
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", Mock(return_value={"text": [], "conf": []}))

    field = make_field(bbox=(0.1, 0.2, 0.5, 0.6))
    field_extraction.extract_field(image, field)

    # relative_bbox_to_pixels((0.1,0.2,0.5,0.6), 1000, 800) = (100,160,500,480),
    # then +/- config.FIELD_CROP_MARGIN_PX on each side. Nowhere near an
    # edge, so no clamping should be in play here - a pure arithmetic check.
    margin = config.FIELD_CROP_MARGIN_PX
    expected_box = (100 - margin, 160 - margin, 500 + margin, 480 + margin)
    image.crop.assert_called_once_with(expected_box)


def test_field_near_edge_crop_is_clamped_not_out_of_bounds(monkeypatch):
    image = Image.new("RGB", (200, 150), color=255)
    image.crop = Mock(wraps=image.crop)
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", Mock(return_value={"text": [], "conf": []}))

    # Flush against the bottom-right corner - expanding by the margin would
    # push past the image's real bounds if this weren't clamped.
    field = make_field(bbox=(0.9, 0.9, 1.0, 1.0))

    result = field_extraction.extract_field(image, field)  # must not raise

    called_box = image.crop.call_args[0][0]
    left, top, right, bottom = called_box
    assert left >= 0 and top >= 0
    assert right <= image.width and bottom <= image.height
    # relative_bbox_to_pixels(...) = (180,135,200,150). Expanded by whatever
    # margin is configured, the right/bottom edges would run past the image's
    # real 200x150 bounds; clamping is what keeps them on it. Computed from
    # the config value rather than hardcoded, since the margin is a measured
    # tunable and this test is about the clamping, not about its size.
    margin = config.FIELD_CROP_MARGIN_PX
    assert called_box == (180 - margin, 135 - margin, 200, 150)
    assert result.raw_text == ""


def test_string_field_uses_psm_7(monkeypatch):
    image = Image.new("RGB", (500, 500), color=255)
    mock_ocr = Mock(return_value={"text": [], "conf": []})
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", mock_ocr)

    field = make_field(bbox=(0.1, 0.1, 0.4, 0.2), data_type="string")
    result = field_extraction.extract_field(image, field)

    assert result.psm_used == 7
    assert mock_ocr.call_args.kwargs["config"] == "--psm 7"


def test_enum_field_uses_psm_8(monkeypatch):
    image = Image.new("RGB", (500, 500), color=255)
    mock_ocr = Mock(return_value={"text": [], "conf": []})
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", mock_ocr)

    field = make_field(bbox=(0.1, 0.1, 0.4, 0.2), data_type="enum")
    result = field_extraction.extract_field(image, field)

    assert result.psm_used == 8
    assert mock_ocr.call_args.kwargs["config"] == "--psm 8"


def test_unrecognized_data_type_falls_back_to_psm_6_not_7(monkeypatch):
    image = Image.new("RGB", (500, 500), color=255)
    mock_ocr = Mock(return_value={"text": [], "conf": []})
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", mock_ocr)

    field = make_field(bbox=(0.1, 0.1, 0.4, 0.2), data_type="date")  # not in PSM_BY_DATA_TYPE
    result = field_extraction.extract_field(image, field)

    assert result.psm_used == 6
    assert result.psm_used != 7  # proves this is the real fallback branch, not an accidental default
    assert mock_ocr.call_args.kwargs["config"] == "--psm 6"


def test_confidence_aggregation_excludes_negative_one_entries():
    # Tesseract marks structural rows (page/block/para/line, not real
    # words) with empty text and conf=-1, interleaved with real words.
    data = {"text": ["Hello", "", "World", ""], "conf": [95.0, -1, 85.0, -1]}

    words, confidences = field_extraction._words_and_confidences(data)

    assert words == ["Hello", "World"]
    assert confidences == [95.0, 85.0]
    assert field_extraction._aggregate_confidence(confidences) == 90.0


def test_full_run_returns_one_result_per_field_keyed_correctly(client, unique_doc_type, monkeypatch):
    resp = client.post(
        "/templates",
        json={
            "template_id": unique_doc_type,
            "anchors": [{"text": "X", "bbox": [0.01, 0.01, 0.2, 0.05], "match_threshold": 0.5}],
            "fields": [
                {
                    "id": "name",
                    "label": "Name",
                    "bbox": [0.1, 0.2, 0.5, 0.3],
                    "data_type": "string",
                    "validation_regex": None,
                    "confidence_floor": 0.75,
                },
                {
                    "id": "code",
                    "label": "Code",
                    "bbox": [0.1, 0.4, 0.3, 0.5],
                    "data_type": "enum",
                    "validation_regex": None,
                    "confidence_floor": 0.8,
                },
            ],
        },
    )
    assert resp.status_code == 201
    version = resp.json()["version"]

    monkeypatch.setattr(
        field_extraction.pytesseract,
        "image_to_data",
        Mock(
            side_effect=[
                {"text": ["John", "Doe"], "conf": [90, 92]},
                {"text": ["O+"], "conf": [88]},
            ]
        ),
    )

    results = field_extraction.extract_fields(unique_doc_type, version, blank_image_bytes())

    assert set(results.keys()) == {"name", "code"}
    assert results["name"].raw_text == "John Doe"
    assert results["name"].psm_used == 7
    assert results["name"].aggregate_confidence == 91.0
    assert results["code"].raw_text == "O+"
    assert results["code"].psm_used == 8
