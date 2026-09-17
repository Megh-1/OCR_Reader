import io
from unittest.mock import Mock
from uuid import uuid4

import pytest
from PIL import Image

import app.config as config
import app.modules.type_identification as type_identification
from app.modules.type_identification import identify_document_type
from app.schemas.template import TemplateAnchor
from app.utils.geometry import relative_bbox_to_pixels


def blank_image_bytes(size: tuple[int, int] = (200, 200)) -> bytes:
    """A plain placeholder page image. Its actual pixel content never
    matters in these tests - pytesseract.image_to_string is always mocked,
    so no real OCR runs against it."""
    buf = io.BytesIO()
    Image.new("RGB", size, color=255).save(buf, format="PNG")
    return buf.getvalue()


def save_template(client, template_id, anchors):
    resp = client.post("/templates", json={"template_id": template_id, "anchors": anchors, "fields": []})
    assert resp.status_code == 201
    return resp.json()


def test_relative_bbox_to_pixels_is_exact():
    assert relative_bbox_to_pixels((0.1, 0.2, 0.5, 0.6), 1000, 500) == (100, 100, 500, 300)


def test_correct_hint_all_anchors_match_confirms(client, unique_doc_type, monkeypatch):
    save_template(
        client,
        unique_doc_type,
        anchors=[
            {"text": "BITS PILANI", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85},
            {"text": "Blood Group", "bbox": [0.05, 0.55, 0.30, 0.62], "match_threshold": 0.85},
        ],
    )
    monkeypatch.setattr(
        type_identification.pytesseract, "image_to_string", Mock(side_effect=["BITS PILANI", "Blood Group"])
    )

    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is True
    assert result.template_id == unique_doc_type
    assert result.version == 1
    assert result.reason is None
    assert result.per_anchor_scores[unique_doc_type]["BITS PILANI"] == 100.0
    assert result.per_anchor_scores[unique_doc_type]["Blood Group"] == 100.0


def test_correct_hint_wrong_image_rejects(client, unique_doc_type, monkeypatch):
    save_template(
        client,
        unique_doc_type,
        anchors=[{"text": "BITS PILANI", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85}],
    )
    monkeypatch.setattr(
        type_identification.pytesseract, "image_to_string", Mock(return_value="COMPLETELY UNRELATED GARBAGE TEXT")
    )

    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is False
    assert result.reason == "no_match"
    assert result.template_id is None


def test_hint_for_unknown_doc_type_rejects_without_scanning(unique_doc_type, monkeypatch):
    spy = Mock(wraps=type_identification.list_known_doc_types)
    monkeypatch.setattr(type_identification, "list_known_doc_types", spy)

    # unique_doc_type was never saved, so this hint cannot resolve.
    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is False
    assert result.reason == "unknown_type_hint"
    assert result.template_id is None
    spy.assert_not_called()


def test_one_of_two_anchors_failing_rejects_whole_template(client, unique_doc_type, monkeypatch):
    save_template(
        client,
        unique_doc_type,
        anchors=[
            {"text": "BITS PILANI", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85},
            {"text": "Blood Group", "bbox": [0.05, 0.55, 0.30, 0.62], "match_threshold": 0.85},
        ],
    )
    # First anchor reads perfectly; second comes back as noise.
    monkeypatch.setattr(
        type_identification.pytesseract, "image_to_string", Mock(side_effect=["BITS PILANI", "xyzzy qqq 999"])
    )

    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is False
    assert result.reason == "no_match"
    scores = result.per_anchor_scores[unique_doc_type]
    assert scores["BITS PILANI"] == 100.0
    assert scores["Blood Group"] < 85.0


def test_no_hint_matches_exactly_one_of_several(client, monkeypatch):
    doc_a, doc_b = f"doc_a_{uuid4().hex}", f"doc_b_{uuid4().hex}"
    save_template(client, doc_a, anchors=[{"text": "ANCHOR_A", "bbox": [0.05, 0.05, 0.4, 0.15], "match_threshold": 0.85}])
    save_template(client, doc_b, anchors=[{"text": "ANCHOR_B", "bbox": [0.05, 0.05, 0.4, 0.15], "match_threshold": 0.85}])

    # Scope the "no hint" scan to just these two, so leftover templates
    # from unrelated tests sharing this database can't affect the outcome.
    monkeypatch.setattr(type_identification, "list_known_doc_types", lambda: [doc_a, doc_b])
    monkeypatch.setattr(
        type_identification.pytesseract, "image_to_string", Mock(side_effect=["ANCHOR_A", "not a match at all"])
    )

    result = identify_document_type(blank_image_bytes())

    assert result.confirmed is True
    assert result.template_id == doc_a
    assert result.version == 1
    assert result.reason is None


def test_no_hint_matches_none(client, monkeypatch):
    doc_a, doc_b = f"doc_a_{uuid4().hex}", f"doc_b_{uuid4().hex}"
    save_template(client, doc_a, anchors=[{"text": "ANCHOR_A", "bbox": [0.05, 0.05, 0.4, 0.15], "match_threshold": 0.85}])
    save_template(client, doc_b, anchors=[{"text": "ANCHOR_B", "bbox": [0.05, 0.05, 0.4, 0.15], "match_threshold": 0.85}])

    monkeypatch.setattr(type_identification, "list_known_doc_types", lambda: [doc_a, doc_b])
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="matches nothing here"))

    result = identify_document_type(blank_image_bytes())

    assert result.confirmed is False
    assert result.reason == "no_match"
    assert result.template_id is None


def test_no_hint_matching_two_templates_is_ambiguous_not_a_guess(client, monkeypatch):
    doc_a, doc_b = f"doc_a_{uuid4().hex}", f"doc_b_{uuid4().hex}"
    # Generously low threshold + identical anchor text: both templates
    # genuinely pass simultaneously against the same OCR result.
    anchor = {"text": "SAME TEXT", "bbox": [0.05, 0.05, 0.4, 0.15], "match_threshold": 0.5}
    save_template(client, doc_a, anchors=[anchor])
    save_template(client, doc_b, anchors=[anchor])

    monkeypatch.setattr(type_identification, "list_known_doc_types", lambda: [doc_a, doc_b])
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="SAME TEXT"))

    result = identify_document_type(blank_image_bytes())

    assert result.confirmed is False
    assert result.reason == "ambiguous_match"
    assert result.template_id is None  # must not arbitrarily pick doc_a or doc_b
    assert set(result.per_anchor_scores.keys()) == {doc_a, doc_b}


def test_anchor_crop_has_exact_margin_applied(monkeypatch):
    # Discovered against a real photo: a zero-slack anchor crop is fragile
    # against Tesseract's page segmentation getting thrown off by a few
    # stray edge pixels (residual tilt, rounding) - config.ANCHOR_CROP_MARGIN_PX
    # exists specifically to absorb that.
    image = Image.new("RGB", (1000, 800), color=255)
    image.crop = Mock(wraps=image.crop)
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value=""))

    anchor = TemplateAnchor(text="HEADER", bbox=(0.1, 0.2, 0.5, 0.6), match_threshold=0.85)
    type_identification._score_anchor(image, anchor)

    # relative_bbox_to_pixels((0.1,0.2,0.5,0.6), 1000, 800) = (100,160,500,480),
    # then +/- config.ANCHOR_CROP_MARGIN_PX on each side. Nowhere near an
    # edge, so no clamping should be in play here - a pure arithmetic check.
    margin = config.ANCHOR_CROP_MARGIN_PX
    expected_box = (100 - margin, 160 - margin, 500 + margin, 480 + margin)
    image.crop.assert_called_once_with(expected_box)


def test_anchor_crop_near_edge_is_clamped_not_out_of_bounds(monkeypatch):
    image = Image.new("RGB", (200, 150), color=255)
    image.crop = Mock(wraps=image.crop)
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value=""))

    # Flush against the top-left corner - expanding by the margin would
    # push past (0,0) if this weren't clamped.
    anchor = TemplateAnchor(text="HEADER", bbox=(0.0, 0.0, 0.2, 0.1), match_threshold=0.85)
    type_identification._score_anchor(image, anchor)  # must not raise

    called_box = image.crop.call_args[0][0]
    left, top, right, bottom = called_box
    assert left >= 0 and top >= 0
    assert right <= image.width and bottom <= image.height
