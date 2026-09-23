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


def ocr_responses(*per_field):
    """Repeats each field's mocked OCR response once per read extract_field will
    make of it, since it reads each crop at several segmentation modes and keeps
    the most confident. Each entry is (data_type, response) or, for a field whose
    validation_regex admits only digits - which earns one extra digit-whitelisted
    read - (data_type, response, validation_regex). Derived from config and from
    extract_field's own digit-only test rather than hardcoded, so adding a
    candidate mode does not silently break these tests."""
    from app.config import DEFAULT_PSM, PSM_BY_DATA_TYPE, PSM_CANDIDATES

    out = []
    for data_type, response, *regex in per_field:
        declared = PSM_BY_DATA_TYPE.get(data_type, DEFAULT_PSM)
        reads = len([declared] + [p for p in PSM_CANDIDATES if p != declared])
        if regex and field_extraction._accepts_only_digits(regex[0]):
            reads += 1
        out.extend([response] * reads)
    return out


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
    assert mock_ocr.call_args_list[0].kwargs["config"] == "--psm 7"  # declared mode tried first


def test_enum_field_uses_psm_8(monkeypatch):
    image = Image.new("RGB", (500, 500), color=255)
    mock_ocr = Mock(return_value={"text": [], "conf": []})
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", mock_ocr)

    field = make_field(bbox=(0.1, 0.1, 0.4, 0.2), data_type="enum")
    result = field_extraction.extract_field(image, field)

    assert result.psm_used == 8
    assert mock_ocr.call_args_list[0].kwargs["config"] == "--psm 8"  # declared mode tried first


def test_unrecognized_data_type_falls_back_to_psm_6_not_7(monkeypatch):
    image = Image.new("RGB", (500, 500), color=255)
    mock_ocr = Mock(return_value={"text": [], "conf": []})
    monkeypatch.setattr(field_extraction.pytesseract, "image_to_data", mock_ocr)

    field = make_field(bbox=(0.1, 0.1, 0.4, 0.2), data_type="date")  # not in PSM_BY_DATA_TYPE
    result = field_extraction.extract_field(image, field)

    assert result.psm_used == 6
    assert result.psm_used != 7  # proves this is the real fallback branch, not an accidental default
    assert mock_ocr.call_args_list[0].kwargs["config"] == "--psm 6"  # declared mode tried first


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
            side_effect=ocr_responses(
                ("string", {"text": ["John", "Doe"], "conf": [90, 92]}),
                ("enum", {"text": ["O+"], "conf": [88]}),
            )
        ),
    )

    results = field_extraction.extract_fields(unique_doc_type, version, blank_image_bytes())

    assert set(results.keys()) == {"name", "code"}
    assert results["name"].raw_text == "John Doe"
    assert results["name"].psm_used == 7
    assert results["name"].aggregate_confidence == 91.0
    assert results["code"].raw_text == "O+"
    assert results["code"].psm_used == 8


def test_many_fields_extract_concurrently_without_racing_on_image_decode(client, unique_doc_type, monkeypatch):
    """Regression guard for a real crash, not a hypothetical one.

    extract_fields reads fields on a thread pool. PIL decodes lazily, so the
    first crop() is what actually loads the image - and with several workers
    entering load() at once on the same freshly-opened object, PIL died on an
    internal assertion. Enough fields here to guarantee the pool runs more
    than one worker concurrently.
    """
    fields = [
        {
            "id": f"field_{i}", "label": f"Field {i}",
            "bbox": [0.05, 0.02 + i * 0.03, 0.45, 0.04 + i * 0.03],
            "data_type": "string", "validation_regex": None, "confidence_floor": 0.75,
        }
        for i in range(20)
    ]
    resp = client.post(
        "/templates",
        json={
            "template_id": unique_doc_type,
            "anchors": [{"text": "X", "bbox": [0.0, 0.0, 0.1, 0.1], "match_threshold": 0.85}],
            "fields": fields,
        },
    )
    assert resp.status_code == 201
    version = resp.json()["version"]

    monkeypatch.setattr(
        field_extraction.pytesseract,
        "image_to_data",
        Mock(return_value={"text": ["VALUE"], "conf": [90]}),
    )

    results = field_extraction.extract_fields(unique_doc_type, version, blank_image_bytes((400, 900)))

    assert set(results.keys()) == {f"field_{i}" for i in range(20)}
    assert all(r.raw_text == "VALUE" for r in results.values())
