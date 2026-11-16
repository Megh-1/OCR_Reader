"""The Review highlight: which words on the page a value's source text matches (app/modules/locate.py). Display only."""

import app.modules.locate as locate_mod
from app.modules.locate import locate

WORDS = [
    ("SHIPPER", (0.05, 0.10, 0.12, 0.12)), ("TRICON", (0.05, 0.13, 0.10, 0.15)), ("ENTERPRISES", (0.11, 0.13, 0.20, 0.15)),
    ("PVT.", (0.21, 0.13, 0.24, 0.15)), ("LTD.,", (0.25, 0.13, 0.28, 0.15)), ("MUMBAI", (0.05, 0.16, 0.11, 0.18)),
    ("176-06067736", (0.60, 0.05, 0.75, 0.07)),
]


def test_a_value_on_one_line_is_boxed_as_one_line():
    assert locate(WORDS, "176-06067736") == [[0.60, 0.05, 0.75, 0.07]]


def test_a_value_over_two_lines_gets_one_box_per_line():
    boxes = locate(WORDS, "TRICON ENTERPRISES PVT. LTD., MUMBAI")
    assert len(boxes) == 2 and boxes[0][0] == 0.05 and boxes[0][2] == 0.28 and boxes[1][1] == 0.16


def test_a_slightly_misread_value_still_points_at_the_closest_words():
    assert locate(WORDS, "176-0606773") == [[0.60, 0.05, 0.75, 0.07]]


def test_nothing_similar_on_the_page_is_no_box():
    assert locate(WORDS, "WAYNE INDUSTRIES GOTHAM") == []
    assert locate(WORDS, "") == [] and locate([], "x") == []


def test_a_short_value_needs_a_near_exact_match():
    words = [("by", (0.5, 0.07, 0.54, 0.08)), ("BEIRUT", (0.1, 0.2, 0.2, 0.22))]
    assert locate(words, "BEY") == []  # 'by' is not 'BEY'
    assert locate(words + [("BEY", (0.3, 0.3, 0.35, 0.32))], "BEY") == [[0.3, 0.3, 0.35, 0.32]]


def test_a_value_split_by_the_forms_columns_is_found_word_by_word():
    # reading order interleaves two columns: the consignee's lines are not consecutive OCR words
    words = [("AL", (0.05, 0.30, 0.07, 0.32)), ("ANEZ", (0.08, 0.30, 0.12, 0.32)), ("ISSUING", (0.60, 0.30, 0.70, 0.32)), ("AGENT", (0.71, 0.30, 0.78, 0.32)),
             ("LEBANON", (0.05, 0.33, 0.12, 0.35)), ("BEIRUT", (0.13, 0.33, 0.19, 0.35)), ("TEL", (0.60, 0.33, 0.63, 0.35))]
    boxes = locate(words, "AL ANEZ LEBANON BEIRUT")
    assert boxes and all(b[2] < 0.3 for b in boxes)  # only the left column is marked


def test_the_route_returns_the_boxes_for_a_stored_result(client, monkeypatch):
    from app.db import SessionLocal
    from app.models.extraction_result import ExtractionResultRecord
    from datetime import datetime, timezone

    monkeypatch.setattr(locate_mod, "page_words", lambda _bytes: tuple(WORDS))
    with SessionLocal() as session:
        row = ExtractionResultRecord(
            template_id="air_waybill", template_version=1, extracted_at=datetime.now(timezone.utc), overall_status="complete", page_image=b"x",
            fields={"awb_number": {"value": "176-06067736", "source_text": "176-06067736", "confidence": 99.0, "needs_review": False, "approval_tier": "auto", "reasons": []}},
        )
        session.add(row)
        session.commit()
        result_id = row.id
    body = client.get(f"/results/{result_id}/locate", params={"field_id": "awb_number"}).json()
    assert body["boxes"] == [[0.60, 0.05, 0.75, 0.07]]
    assert client.get(f"/results/{result_id}/locate", params={"field_id": "nope"}).status_code == 404
    assert client.get("/results/999999/locate", params={"field_id": "x"}).status_code == 404
