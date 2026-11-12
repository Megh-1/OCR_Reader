"""Page alignment (app/modules/alignment.py) and where Modules 5, 6 and the landmark builder use it."""

import random
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

import app.modules.type_identification as type_identification
from app.models.text_layer import TextLayer, TextWord
from app.modules.alignment import PageWord, fit_alignment
from app.modules.field_extraction import extract_fields_from_text_layer
from app.modules.template_store import get_latest_template
from app.modules.type_identification import identify_document_type
from app.schemas.template import TemplateAnchor, TemplateLandmark, TemplateOut
from scripts import build_landmarks

from tests.test_type_identification import blank_image_bytes

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 40 distinct printed-looking words, spread over the page
_WORDS = [
    "shipper", "consignee", "airport", "departure", "destination", "currency", "handling", "information",
    "nature", "quantity", "goods", "dimensions", "volume", "charge", "weight", "chargeable", "commodity",
    "carrier", "declared", "customs", "insurance", "amount", "routing", "issuing", "agent", "account",
    "accounting", "optional", "reference", "signature", "total", "prepaid", "collect", "valuation",
    "other", "charges", "executed", "place", "certifies", "particulars",
]


def template_landmarks(n=40, seed=3):
    rnd = random.Random(seed)
    return [TemplateLandmark(text=_WORDS[i], x=rnd.uniform(0.05, 0.85), y=rnd.uniform(0.05, 0.95)) for i in range(n)]


def page_for(landmarks, sx=0.95, tx=-0.03, sy=0.91, ty=0.04, jitter=0.002, data=40, wrong=5, seed=5):
    """The words OCR would find on a page whose form sits at page = s * template + t: every landmark,
    jittered, plus `data` words that are not landmarks and `wrong` landmark words in the wrong places."""
    rnd = random.Random(seed)
    words = [
        PageWord(m.text, sx * m.x + tx + rnd.uniform(-jitter, jitter), sy * m.y + ty + rnd.uniform(-jitter, jitter))
        for m in landmarks
    ]
    words += [PageWord(f"datum{i}word", rnd.random(), rnd.random()) for i in range(data)]
    words += [PageWord(m.text, rnd.random(), rnd.random()) for m in rnd.sample(landmarks, wrong)]
    return words


def test_recovers_a_known_shift_and_stretch_despite_data_words_and_misplaced_matches():
    lm = template_landmarks()
    a = fit_alignment(lm, page_for(lm))

    assert a is not None
    assert (a.sx, a.tx, a.sy, a.ty) == pytest.approx((0.95, -0.03, 0.91, 0.04), abs=0.01)
    assert a.landmarks >= 35
    assert a.rms < 0.01


def test_map_box_moves_a_box_by_the_fitted_transform():
    lm = template_landmarks()
    a = fit_alignment(lm, page_for(lm, jitter=0.0))

    x0, y0, x1, y1 = a.map_box((0.2, 0.3, 0.4, 0.35))

    assert (x0, y0, x1, y1) == pytest.approx((0.95 * 0.2 - 0.03, 0.91 * 0.3 + 0.04, 0.95 * 0.4 - 0.03, 0.91 * 0.35 + 0.04), abs=0.004)


def test_a_box_follows_the_landmarks_nearest_to_it_when_the_form_is_not_uniformly_stretched():
    lm = template_landmarks(40)
    words = page_for(lm, jitter=0.0, data=0, wrong=0)
    # the form's lower-right block sits 0.012 lower than one stretch explains (inside the RANSAC tolerance)
    corner = [i for i, m in enumerate(lm) if m.x > 0.55 and m.y > 0.55]
    assert len(corner) >= 3
    for i in corner:
        words[i] = PageWord(words[i].text, words[i].x, words[i].y + 0.012)
    a = fit_alignment(lm, words)

    near = lm[corner[0]]
    box = (near.x - 0.01, near.y - 0.01, near.x + 0.01, near.y + 0.01)
    far = (0.06, 0.06, 0.08, 0.08)
    global_y = lambda b: 0.91 * b[1] + 0.04  # noqa: E731

    assert a.map_box(box)[1] - global_y(box) == pytest.approx(0.012, abs=0.004)
    assert abs(a.map_box(far)[1] - global_y(far)) < 0.006


def test_refuses_rather_than_guesses_when_too_few_landmarks_agree():
    lm = template_landmarks()
    words = page_for(lm, wrong=0)[:8] + [PageWord(w.text, random.random(), random.random()) for w in page_for(lm)[8:40]]

    assert fit_alignment(lm, words) is None


def test_refuses_a_fit_that_needs_an_absurd_stretch():
    lm = template_landmarks()

    assert fit_alignment(lm, page_for(lm, sy=1.6, ty=-0.3)) is None


def test_the_same_page_always_aligns_the_same_way():
    lm = template_landmarks()
    words = page_for(lm)

    a, b = fit_alignment(lm, words), fit_alignment(lm, words)

    assert (a.sx, a.tx, a.sy, a.ty, a.matched) == (b.sx, b.tx, b.sy, b.ty, b.matched)


def test_ocr_noise_in_a_landmark_word_still_matches():
    lm = template_landmarks()
    words = page_for(lm)
    words = [PageWord(w.text.replace("a", "o", 1), w.x, w.y) if w.text in ("shipper", "airport", "quantity") else w for w in words]

    assert fit_alignment(lm, words) is not None


# --- Module 5 ---------------------------------------------------------------


def save_with_landmarks(client, template_id, landmarks):
    resp = client.post(
        "/templates",
        json={
            "template_id": template_id,
            "anchors": [{"text": "Not Negotiable", "bbox": [0.10, 0.10, 0.40, 0.15], "match_threshold": 0.70}],
            "fields": [],
            "landmarks": [m.model_dump() for m in landmarks],
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_the_anchor_is_read_where_it_sits_on_this_page_and_the_alignment_is_handed_on(client, unique_doc_type, monkeypatch):
    lm = template_landmarks()
    save_with_landmarks(client, unique_doc_type, lm)
    words = page_for(lm, jitter=0.0)
    monkeypatch.setattr(type_identification, "read_page_words", lambda image: words)
    stored_y0 = 0.10
    seen = []

    def score(image, anchor):
        seen.append(anchor.bbox)
        return 100.0 if abs(anchor.bbox[1] - (0.91 * stored_y0 + 0.04)) < 0.01 else 0.0

    monkeypatch.setattr(type_identification, "_score_anchor", score)

    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is True
    assert result.alignment is not None and result.alignment.landmarks >= 35
    assert seen[0][1] > 0.10  # the stored box was moved, not used as is


def test_without_landmarks_the_stored_box_is_used_exactly_as_before(client, unique_doc_type, monkeypatch):
    save_with_landmarks(client, unique_doc_type, [])
    read_words = Mock(return_value=[])
    monkeypatch.setattr(type_identification, "read_page_words", read_words)
    seen = []
    monkeypatch.setattr(type_identification, "_score_anchor", lambda image, anchor: seen.append(anchor.bbox) or 100.0)

    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is True and result.alignment is None
    assert seen == [(0.10, 0.10, 0.40, 0.15)]
    read_words.assert_not_called()  # no page OCR is spent on a template that has no landmarks


def test_a_page_whose_landmarks_do_not_line_up_is_read_at_the_stored_position(client, unique_doc_type, monkeypatch):
    lm = template_landmarks()
    save_with_landmarks(client, unique_doc_type, lm)
    monkeypatch.setattr(type_identification, "read_page_words", lambda image: [PageWord("unrelated", 0.5, 0.5)])
    seen = []
    monkeypatch.setattr(type_identification, "_score_anchor", lambda image, anchor: seen.append(anchor.bbox) or 100.0)

    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is True and result.alignment is None
    assert seen == [(0.10, 0.10, 0.40, 0.15)]


def test_an_aligned_anchor_that_misses_refuses_the_page_even_if_the_stored_position_would_pass(client, unique_doc_type, monkeypatch):
    """The boxes that verify a page are the boxes that read it: no passing at one position and reading at another."""
    lm = template_landmarks()
    save_with_landmarks(client, unique_doc_type, lm)
    monkeypatch.setattr(type_identification, "read_page_words", lambda image: page_for(lm, jitter=0.0))
    monkeypatch.setattr(type_identification, "_score_anchor", lambda image, anchor: 100.0 if anchor.bbox == (0.10, 0.10, 0.40, 0.15) else 0.0)

    result = identify_document_type(blank_image_bytes(), type_hint=unique_doc_type)

    assert result.confirmed is False and result.reason == "no_match"


def test_anchor_text_matches_whatever_the_case_and_line_breaks(monkeypatch):
    monkeypatch.setattr(type_identification.pytesseract, "image_to_string", Mock(return_value="NOT negotiable\nAIR waybill "))
    anchor = TemplateAnchor(text="Not Negotiable Air Waybill", bbox=(0.1, 0.1, 0.4, 0.15), match_threshold=0.7)

    assert type_identification._score_anchor(Image.new("RGB", (200, 200), 255), anchor) == 100.0


# --- Module 6 ---------------------------------------------------------------


def test_text_layer_fields_are_read_where_the_page_puts_them(client, unique_doc_type):
    lm = template_landmarks()
    resp = client.post(
        "/templates",
        json={
            "template_id": unique_doc_type,
            "anchors": [{"text": "Not Negotiable", "bbox": [0.1, 0.1, 0.4, 0.15], "match_threshold": 0.7}],
            "fields": [{"id": "shipper", "label": "Shipper", "bbox": [0.20, 0.30, 0.24, 0.32], "data_type": "string", "confidence_floor": 0.6}],
        },
    )
    assert resp.status_code == 201
    a = fit_alignment(lm, page_for(lm, jitter=0.0))
    moved = a.map_box((0.20, 0.30, 0.24, 0.32))
    layer = TextLayer(words=(TextWord("ACME", (moved[0] + 0.003, moved[1] + 0.003, moved[2] - 0.003, moved[3] - 0.003)),))

    where_it_is = extract_fields_from_text_layer(unique_doc_type, 1, layer, a)["shipper"]
    where_stored = extract_fields_from_text_layer(unique_doc_type, 1, layer)["shipper"]

    assert where_it_is.raw_text == "ACME"
    assert where_stored.raw_text == ""  # the old behaviour, still there when no alignment is given


# --- storage ----------------------------------------------------------------


def test_landmarks_are_stored_with_the_template_and_old_versions_load_without_them(client, unique_doc_type):
    lm = template_landmarks(5)
    save_with_landmarks(client, unique_doc_type, lm)

    stored = get_latest_template(unique_doc_type)
    assert [m.text for m in stored.landmarks] == [m.text for m in lm]

    old_config = {k: v for k, v in stored.model_dump(mode="json").items() if k != "landmarks"}
    assert TemplateOut(**old_config).landmarks == []


# --- the landmark builder ---------------------------------------------------


def test_the_builder_keeps_form_words_that_recur_and_drops_data_words(monkeypatch):
    form = template_landmarks(40)
    ref = [PageWord(m.text, m.x, m.y) for m in form] + [PageWord(f"datum{i}word", random.Random(i).random(), random.Random(i + 9).random()) for i in range(15)]
    rnd = random.Random(11)

    def words_for(path):
        if path.name == "ref.pdf":
            return ref
        subset = rnd.sample(form, 32)  # each other document carries most, not all, of the printed words
        shifted = page_for(subset, sx=rnd.uniform(0.93, 1.04), tx=rnd.uniform(-0.04, 0.03), sy=rnd.uniform(0.9, 0.97), ty=rnd.uniform(0.0, 0.04), data=0, wrong=0, seed=rnd.randint(0, 99))
        return shifted + [PageWord(f"zzdata{i}x", rnd.random(), rnd.random()) for i in range(25)]  # that document's own data

    monkeypatch.setattr(build_landmarks, "page_words", words_for)

    kept, notes = build_landmarks.build(Path("ref.pdf"), [Path(f"d{i}.pdf") for i in range(6)], min_docs=3, min_landmarks=10)

    kept_words = {k["text"] for k in kept}
    assert not any(w.startswith("datum") or w.startswith("zzdata") for w in kept_words)
    assert len(kept_words & {m.text for m in form}) >= 35


def test_the_builder_does_not_count_a_document_that_does_not_align(monkeypatch):
    form = template_landmarks(40)
    ref = [PageWord(m.text, m.x, m.y) for m in form]
    stranger = [PageWord(f"nothing{i}here", random.Random(i).random(), random.Random(i + 5).random()) for i in range(60)]
    monkeypatch.setattr(build_landmarks, "page_words", lambda p: ref if p.name == "ref.pdf" else stranger)

    kept, notes = build_landmarks.build(Path("ref.pdf"), [Path(f"d{i}.pdf") for i in range(4)], min_docs=1, min_landmarks=10)

    assert kept == []
    assert any("does not align" in n for n in notes)


# --- the shipped landmark file and the real documents -----------------------


def test_the_shipped_landmarks_are_printed_form_words_only():
    import json

    words = {d["text"] for d in json.loads((PROJECT_ROOT / "scripts" / "landmarks" / "air_waybill.json").read_text(encoding="utf-8"))}

    assert len(words) >= 60
    assert {"waybill", "consignees", "handling", "nature", "quantity"} <= words
    # nothing from the reference document's own identity may be in a committed file
    assert not words & {"teamglobal", "kanekal", "ihsanullah", "amanatali", "bangalore", "dubai"}


REAL = PROJECT_ROOT / "data" / "real_awb"


@pytest.mark.skipif(not (REAL / "Easy").exists(), reason="real waybills are untracked data, not in the repo")
@pytest.mark.parametrize(
    "relative",
    [
        "Easy/176066913222025-02-26-16-44AWBUploadDocument.pdf",  # A4 text-native, a different printer from the reference
        "Medium/057544027622025-01-30-09-49AWBUploadDocument.pdf",  # image-only scan
    ],
)
def test_real_waybills_that_were_refused_before_alignment_are_now_recognised(relative):
    from app.modules import pipeline, preprocessing
    from scripts.seed_templates import AIR_WAYBILL, seed

    seed([AIR_WAYBILL])
    pages, _meta, layers = pipeline.prepare_document((REAL / relative).read_bytes(), Path(relative).name, None)
    generic = preprocessing.preprocess_image(pages[0], deskew=layers[0] is None)

    result = identify_document_type(generic.image_bytes, type_hint="air_waybill")

    assert result.confirmed is True
    assert result.alignment is not None and result.alignment.landmarks >= 12


# --- the local correction (config.ALIGN_LOCAL_*) -------------------------------------------------------------------------


def alignment_with(rows):
    """An identity fit (page = template) whose matched landmarks are `rows` = (template x, y, page x, y)."""
    import numpy as np

    from app.modules.alignment import Alignment

    return Alignment(sx=1.0, tx=0.0, sy=1.0, ty=0.0, landmarks=len(rows), rms=0.0, matched=tuple(range(len(rows))), _rows=np.array(rows))


BOX = (0.40, 0.40, 0.50, 0.42)


def test_a_box_is_moved_by_the_median_of_three_or_more_landmarks_in_range():
    a = alignment_with([(0.45, 0.35, 0.45, 0.36), (0.40, 0.45, 0.40, 0.46), (0.52, 0.41, 0.52, 0.42)])  # each 0.01 lower

    assert a.map_box(BOX)[1] == pytest.approx(0.41, abs=1e-9)


def test_one_mismatched_landmark_cannot_move_a_box_when_others_disagree():
    """A typed word paired with the wrong occurrence of itself (0.019 off) is outvoted by two good pairs."""
    a = alignment_with([(0.45, 0.35, 0.45, 0.369), (0.40, 0.45, 0.40, 0.45), (0.52, 0.41, 0.52, 0.41)])

    assert a.map_box(BOX)[1] == pytest.approx(0.40, abs=1e-9)


def test_fewer_than_three_landmarks_in_range_leave_the_box_on_the_global_fit():
    a = alignment_with([(0.45, 0.35, 0.45, 0.37), (0.40, 0.45, 0.40, 0.47), (0.90, 0.90, 0.90, 0.95)])  # third is out of range

    assert a.map_box(BOX) == pytest.approx(BOX)


def test_a_box_pushed_past_the_page_edge_stays_a_strip_on_the_edge_not_the_stored_box():
    from app.modules.alignment import Alignment
    import numpy as np

    a = Alignment(sx=1.0, tx=0.6, sy=1.0, ty=0.0, landmarks=3, rms=0.0, matched=(0, 1, 2), _rows=np.zeros((3, 4)))

    x0, y0, x1, y1 = a.map_box((0.70, 0.10, 0.80, 0.20))

    assert 0.0 <= x0 < x1 <= 1.0 and x0 > 0.99  # on the right edge, where it will read blank and be flagged
    assert (x0, x1) != (0.70, 0.80)


def test_an_alignment_can_be_hashed_and_compared_like_any_object():
    a = alignment_with([(0.45, 0.35, 0.45, 0.36)])
    b = alignment_with([(0.45, 0.35, 0.45, 0.36)])

    assert {a: 1}[a] == 1
    assert (a == a) is True and (a == b) is False


def test_the_cross_terms_follow_a_tilted_page_when_enabled_and_are_absent_when_off(monkeypatch):
    import app.modules.alignment as AL
    lm = template_landmarks(40)
    tilt = 0.012  # page y rises by 1.2% of the page across its whole width, x shifts the other way: a ~0.7 degree tilt
    words = [PageWord(m.text, m.x - tilt * m.y, m.y + tilt * m.x) for m in lm]
    monkeypatch.setattr(AL, "ALIGN_MAX_CROSS", 0.03)
    a = fit_alignment(lm, words)
    assert (a.kx, a.ky) == pytest.approx((-tilt, tilt), abs=0.002)
    far = (0.80, 0.10, 0.84, 0.12)  # top right: the corner a tilt moves the most vertically
    assert a.map_box(far)[1] == pytest.approx(0.10 + tilt * 0.82, abs=0.002)
    monkeypatch.setattr(AL, "ALIGN_MAX_CROSS", 0.0)
    b = fit_alignment(lm, words)
    assert (b.kx, b.ky) == (0.0, 0.0)
