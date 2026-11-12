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


def test_hint_only_template_is_skipped_by_the_scan_but_works_by_hint(client, monkeypatch):
    full, part = f"doc_a_{uuid4().hex}", f"doc_b_{uuid4().hex}"
    anchor = {"text": "SAME TEXT", "bbox": [0.05, 0.05, 0.4, 0.15], "match_threshold": 0.5}
    save_template(client, full, anchors=[anchor])
    resp = client.post(
        "/templates", json={"template_id": part, "anchors": [anchor], "fields": [], "hints": {"hint_only": True}}
    )
    assert resp.status_code == 201

    monkeypatch.setattr(type_identification, "list_known_doc_types", lambda: [full, part])
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="SAME TEXT"))

    scanned = identify_document_type(blank_image_bytes())
    assert scanned.confirmed is True and scanned.template_id == full  # not ambiguous

    hinted = identify_document_type(blank_image_bytes(), type_hint=part)
    assert hinted.confirmed is True and hinted.template_id == part


@pytest.mark.parametrize("blank", ["", "   "])
def test_saving_a_template_with_blank_anchor_text_is_refused(client, unique_doc_type, blank):
    resp = client.post(
        "/templates",
        json={
            "template_id": unique_doc_type,
            "anchors": [{"text": blank, "bbox": [0.1, 0.1, 0.4, 0.2], "match_threshold": 0.7}],
            "fields": [],
        },
    )
    assert resp.status_code == 422


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


# --------------------------------------------------------------------------- a whole form, a part of it, and a different part


from datetime import date  # noqa: E402

from app.schemas.template import TemplateIn, TemplateOut  # noqa: E402


def _template(template_id, anchors, hint_only=False, is_part=False):
    body = TemplateIn(
        template_id=template_id,
        anchors=[{"text": t, "bbox": b, "match_threshold": 0.8} for t, b in anchors],
        fields=[], hints={"hint_only": hint_only, "is_part": is_part},
    )
    return TemplateOut(version=1, created_at=date.today(), **body.model_dump(mode="json"))


TOP = ("Shipper's Name and Address", (0.05, 0.05, 0.40, 0.08))
TOP2 = ("Shipper's Account Number", (0.45, 0.05, 0.80, 0.08))
BOTTOM = ("Signature of Issuing Carrier", (0.50, 0.90, 0.95, 0.94))


def test_a_whole_form_beats_a_part_cut_from_it():
    whole = (_template("whole", [TOP, TOP2, BOTTOM]), None)
    part = (_template("part", [TOP, TOP2], is_part=True), None)
    for order in ([whole, part], [part, whole]):
        assert type_identification._most_specific(order)[0].template_id == "whole"


def test_two_unrelated_forms_that_both_pass_stay_ambiguous():
    a = (_template("vendor_a", [TOP, BOTTOM]), None)
    b = (_template("vendor_b", [TOP2, BOTTOM]), None)
    assert type_identification._most_specific([a, b]) is None


def test_equal_anchor_sets_stay_ambiguous():
    a = (_template("one", [TOP, TOP2]), None)
    b = (_template("two", [TOP, TOP2]), None)
    assert type_identification._most_specific([a, b]) is None


def test_a_part_drawn_on_its_own_page_shares_no_anchor_with_the_whole():
    """The half-page template: its anchors sit in ITS page's frame, so it is nobody's subset and never outranked."""
    whole = (_template("whole", [TOP, TOP2, BOTTOM]), None)
    half_page = (_template("half_page", [("Shipper's Name and Address", (0.05, 0.10, 0.60, 0.16)), TOP2], is_part=True), None)
    assert type_identification._most_specific([whole, half_page]) is None


# --------------------------------------------------------------------------- yielding to a more specific print


def _yielding(template_id, yields_to):
    body = TemplateIn(template_id=template_id, anchors=[{"text": "X", "bbox": (0.1, 0.1, 0.3, 0.2), "match_threshold": 0.8}],
                      fields=[], hints={"yields_to": yields_to})
    return (TemplateOut(version=1, created_at=date.today(), **body.model_dump(mode="json")), None)


def test_a_template_that_yields_stands_down_only_for_a_template_that_also_matched():
    generic, vendor = _yielding("generic", ["vendor"]), _yielding("vendor", [])
    assert [t.template_id for t, _ in type_identification._after_yielding([generic, vendor])] == ["vendor"]
    # the vendor's own anchors did not pass: nothing to yield to, the generic one keeps its match
    assert [t.template_id for t, _ in type_identification._after_yielding([generic])] == ["generic"]


def test_yielding_never_removes_a_match_to_a_template_it_did_not_name():
    generic, other = _yielding("generic", ["vendor"]), _yielding("other", [])
    assert {t.template_id for t, _ in type_identification._after_yielding([generic, other])} == {"generic", "other"}


def test_two_templates_yielding_to_each_other_stay_ambiguous_rather_than_both_vanishing():
    a, b = _yielding("a", ["b"]), _yielding("b", ["a"])
    assert len(type_identification._after_yielding([a, b])) == 2


def test_a_template_cannot_yield_to_itself(client, unique_doc_type):
    body = {"template_id": unique_doc_type, "anchors": [{"text": "Shipper", "bbox": [0.1, 0.1, 0.4, 0.2], "match_threshold": 0.8}],
            "fields": [], "hints": {"yields_to": [unique_doc_type]}}
    assert client.post("/templates", json=body).status_code == 422


def test_the_scan_gives_the_page_to_the_specific_print_and_names_what_it_stood_down_from(client, monkeypatch):
    generic, vendor = f"generic_{uuid4().hex}", f"vendor_{uuid4().hex}"
    anchor = {"text": "SAME TEXT", "bbox": [0.05, 0.05, 0.4, 0.15], "match_threshold": 0.5}
    resp = client.post("/templates", json={"template_id": generic, "anchors": [anchor], "fields": [], "hints": {"yields_to": [vendor]}})
    assert resp.status_code == 201
    save_template(client, vendor, anchors=[anchor])
    monkeypatch.setattr(type_identification, "list_known_doc_types", lambda: [generic, vendor])
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="SAME TEXT"))

    result = identify_document_type(blank_image_bytes())
    assert result.confirmed is True and result.template_id == vendor and result.also_matched == [generic]

    # the vendor's mark is not on the page: the generic template alone matches, as before
    monkeypatch.setattr(type_identification, "list_known_doc_types", lambda: [generic])
    assert identify_document_type(blank_image_bytes()).template_id == generic

    # a hint checks the one template named and ignores the yielding rule
    assert identify_document_type(blank_image_bytes(), type_hint=generic).template_id == generic


def test_part_template_left_to_automatic_detection_needs_two_anchors(client, unique_doc_type):
    one = {"template_id": unique_doc_type, "anchors": [{"text": "Shipper", "bbox": [0.1, 0.1, 0.4, 0.2], "match_threshold": 0.8}],
           "fields": [], "hints": {"is_part": True}}
    assert client.post("/templates", json=one).status_code == 422
    one["hints"] = {"is_part": True, "hint_only": True}  # chosen explicitly: one anchor is enough
    assert client.post("/templates", json=one).status_code == 201


THREE = [
    {"text": "Alpha Label", "bbox": [0.05, 0.02, 0.45, 0.08], "match_threshold": 0.85},
    {"text": "Bravo Label", "bbox": [0.05, 0.30, 0.45, 0.36], "match_threshold": 0.85},
    {"text": "Charlie Label", "bbox": [0.05, 0.60, 0.45, 0.66], "match_threshold": 0.85},
]


def _save_with_min(client, template_id, minimum):
    body = {"template_id": template_id, "anchors": THREE, "fields": [], "hints": {"min_anchors_matching": minimum} if minimum else {}}
    return client.post("/templates", json=body)


def test_min_anchors_lets_one_unreadable_label_through(client, unique_doc_type, monkeypatch):
    assert _save_with_min(client, unique_doc_type, 2).status_code == 201
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(side_effect=["Alpha Label", "smudge ###", "Charlie Label"]))
    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)
    assert result.confirmed is True
    assert result.per_anchor_scores[unique_doc_type]["Bravo Label"] < 85.0  # the miss is still reported


def test_min_anchors_still_refuses_below_the_count(client, unique_doc_type, monkeypatch):
    assert _save_with_min(client, unique_doc_type, 2).status_code == 201
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(side_effect=["Alpha Label", "smudge ###", "more noise"]))
    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)
    assert result.confirmed is False and result.reason == "no_match"


def test_without_min_anchors_every_anchor_is_still_required(client, unique_doc_type, monkeypatch):
    assert _save_with_min(client, unique_doc_type, None).status_code == 201
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(side_effect=["Alpha Label", "smudge ###", "Charlie Label"]))
    assert identify_document_type(blank_image_bytes(), type_hint=unique_doc_type).confirmed is False


@pytest.mark.parametrize("minimum", [1, 4])
def test_min_anchors_must_be_at_least_two_and_at_most_the_anchor_count(client, unique_doc_type, minimum):
    assert _save_with_min(client, unique_doc_type, minimum).status_code == 422


def _anchor_reads(monkeypatch, reads):
    calls = []

    def fake(crop, *a, **k):
        calls.append(crop.size)
        return reads[min(len(calls), len(reads)) - 1]

    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", fake)
    return calls


def test_a_near_miss_anchor_is_read_again_at_offsets_and_keeps_the_best_score(monkeypatch):
    # the label reads 82 at its own crop and 100 at one 1 px over: a threshold of 0.85 passes it only by the retry
    image = Image.new("RGB", (1000, 800), color=255)
    anchor = TemplateAnchor(text="Not Negotiable Air Waybill", bbox=(0.1, 0.2, 0.5, 0.3), match_threshold=0.85)
    calls = _anchor_reads(monkeypatch, ["Not Negotiable Air", "Not Negotiable Air Waybill"])
    assert type_identification._score_anchor(image, anchor) == 100.0
    assert len(calls) == 2


def test_a_passing_anchor_is_read_once(monkeypatch):
    image = Image.new("RGB", (1000, 800), color=255)
    anchor = TemplateAnchor(text="Not Negotiable Air Waybill", bbox=(0.1, 0.2, 0.5, 0.3), match_threshold=0.85)
    calls = _anchor_reads(monkeypatch, ["Not Negotiable Air Waybill"])
    assert type_identification._score_anchor(image, anchor) == 100.0
    assert len(calls) == 1


def test_an_anchor_nowhere_near_its_threshold_is_not_retried(monkeypatch):
    image = Image.new("RGB", (1000, 800), color=255)
    anchor = TemplateAnchor(text="Not Negotiable Air Waybill", bbox=(0.1, 0.2, 0.5, 0.3), match_threshold=0.85)
    calls = _anchor_reads(monkeypatch, ["BANGKOK THAILAND 31 MAR"])
    assert type_identification._score_anchor(image, anchor) < 50
    assert len(calls) == 1


def test_retries_only_ever_add_a_pass_and_stop_after_the_listed_offsets(monkeypatch):
    image = Image.new("RGB", (1000, 800), color=255)
    anchor = TemplateAnchor(text="Not Negotiable Air Waybill", bbox=(0.1, 0.2, 0.5, 0.3), match_threshold=0.99)
    calls = _anchor_reads(monkeypatch, ["Not Negotiable Air Waybil"])  # 98: never reaches 99
    assert 90 < type_identification._score_anchor(image, anchor) < 99
    assert len(calls) == len(config.ANCHOR_OFFSETS_PX)
