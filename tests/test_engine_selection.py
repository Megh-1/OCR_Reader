"""
Engine selection (app/modules/engine_selection, docs/engine-selection.md): the chain chosen per template from measurements.

The rules are tested as PURE functions over fixtures (a registry and the evidence), then the store, the hooks and the pipeline.
"""

import io
import json
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest
from PIL import Image, ImageFilter

from app import config
from app.db import SessionLocal
from app.models.engine_selection import EngineChainLogRecord, LabelledExampleRecord
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord
from app.models.text_layer import TextLayer, TextWord
from app.modules import review_queue
from app.modules.engine_selection import agreement as AG
from app.modules.engine_selection import drift, probe, promotion, selector as S, store
from app.modules.vlm_reader import engines as E
from app.modules.vlm_reader.client import ReaderUnavailable
from app.modules.vlm_reader.decide import Decision

TEXT = probe.DocFeatures(has_text_layer=True, quality="good")
SCAN = probe.DocFeatures(has_text_layer=False, sharpness=120.0, quality="good")


def spec(name, kind="service", **kw):
    return E.EngineSpec(name=name, kind=kind, url=f"http://{name}.test" + ("/v1" if kind == "openai_compatible" else ""),
                        model="m" if kind == "openai_compatible" else "", **kw)


def card(engine, accuracy=0.9, fields=1000, corpus="c1", wrong_auto=0, docs_per_min=2.0, queued_accuracy=None):
    queued = fields // 2
    qa = accuracy if queued_accuracy is None else queued_accuracy
    return S.Card(engine, corpus, fields, round(accuracy * fields), wrong_auto, queued, round(qa * queued), docs_per_min)


def evidence(cards=(), promoted=None, pairs=(), live=None):
    cards = {c.engine: c for c in cards}
    return S.Evidence(cards=cards, promoted=set(cards) if promoted is None else set(promoted), pairs=list(pairs), live=live or {})


# --- cold start ----------------------------------------------------------------------------------------------------------------

def test_cold_start_uses_a_default_chain_and_never_allows_auto():
    specs = [spec("general", "openai_compatible"), spec("tuned", "service")]
    d = S.select_chain("t", SCAN, specs, evidence())
    assert d.mode == "cold_start" and d.force_review
    assert d.readers == ["tuned", "general"]  # the fine-tuned engine first, wherever it sits in the registry
    assert d.chain == ["tuned", "general", "tesseract"]
    assert d.agreement == "caution"  # two readers: a disagreement goes to review, agreement alone is not yet evidence


def test_cold_start_order_does_not_depend_on_how_the_registry_is_arranged():
    a, b = spec("general", "openai_compatible"), spec("tuned", "service")
    assert S.select_chain("t", SCAN, [a, b], evidence()).chain == S.select_chain("t", SCAN, [b, a], evidence()).chain


def test_cold_start_for_a_page_with_a_text_layer_leads_with_it_and_keeps_one_reader_for_doubtful_reads():
    d = S.select_chain("t", TEXT, [spec("tuned"), spec("general", "openai_compatible")], evidence())
    assert d.chain == ["text_layer", "tuned", "tesseract"] and d.agreement == "off" and d.force_review


def test_an_engine_without_a_url_or_model_and_the_pipelines_own_paths_are_never_chosen():
    specs = [E.EngineSpec(name="nourl", kind="service"), E.EngineSpec(name="nomodel", kind="openai_compatible", url="http://x/v1"),
             E.EngineSpec(name="tess", kind="tesseract"), E.EngineSpec(name="tl", kind="text_layer"), spec("ok")]
    assert S.select_chain("t", SCAN, specs, evidence()).readers == ["ok"]


def test_with_no_engine_at_all_the_chain_is_tesseract_alone():
    d = S.select_chain("t", SCAN, [], evidence())
    assert d.chain == ["tesseract"] and d.readers == [] and d.force_review


def test_measured_engines_that_were_never_promoted_do_not_end_cold_start():
    d = S.select_chain("t", SCAN, [spec("a")], evidence([card("a", 0.99)], promoted=[]))
    assert d.mode == "cold_start" and d.numbers["engines"]["a"]["excluded"] == "not promoted"


# --- ranked --------------------------------------------------------------------------------------------------------------------

def test_promoted_engines_are_ranked_by_measured_accuracy_with_tesseract_last():
    specs = [spec("slow_good", "openai_compatible"), spec("fast_ok"), spec("best")]
    ev = evidence([card("slow_good", 0.93), card("fast_ok", 0.88), card("best", 0.96)])
    d = S.select_chain("t", SCAN, specs, ev)
    assert d.mode == "measured" and not d.force_review
    assert d.chain == ["best", "slow_good", "fast_ok", "tesseract"]
    assert d.numbers["engines"]["best"]["accuracy"] == 0.96


def test_ranking_does_not_depend_on_registry_order():
    specs = [spec("a"), spec("b"), spec("c")]
    ev = evidence([card("a", 0.80), card("b", 0.95), card("c", 0.90)])
    assert S.select_chain("t", SCAN, specs, ev).readers == S.select_chain("t", SCAN, list(reversed(specs)), ev).readers == ["b", "c", "a"]


def test_accuracies_inside_the_tie_band_are_ordered_by_speed():
    ev = evidence([card("slow", 0.951, docs_per_min=1.0), card("fast", 0.949, docs_per_min=6.0)])
    assert S.select_chain("t", SCAN, [spec("slow"), spec("fast")], ev).readers == ["fast", "slow"]
    ev = evidence([card("slow", 0.96, docs_per_min=1.0), card("fast", 0.94, docs_per_min=6.0)])  # outside the band: accuracy wins
    assert S.select_chain("t", SCAN, [spec("slow"), spec("fast")], ev).readers == ["slow", "fast"]


def test_a_scorecard_on_too_few_fields_ranks_nothing():
    ev = evidence([card("a", 0.99, fields=config.ENGINE_MIN_FIELDS - 1), card("b", 0.80)])
    assert S.select_chain("t", SCAN, [spec("a"), spec("b")], ev).readers == ["b"]


def test_measured_chain_of_a_text_layer_page_leads_with_the_text_layer():
    assert S.select_chain("t", TEXT, [spec("a")], evidence([card("a")])).chain == ["text_layer", "a", "tesseract"]


def test_agreement_raises_trust_only_for_a_pair_measured_to_be_right_when_it_agrees():
    specs = [spec("a"), spec("b")]
    cards = [card("a", 0.95), card("b", 0.90)]
    good = S.Pair("a", "b", "c1", 1000, 600, 598)  # 0.3% of what they agreed on was wrong
    bad = S.Pair("a", "b", "c1", 1000, 600, 570)  # 5%
    few = S.Pair("a", "b", "c1", 1000, 50, 50)
    assert S.select_chain("t", SCAN, specs, evidence(cards, pairs=[good])).agreement == "trust"
    assert S.select_chain("t", SCAN, specs, evidence(cards, pairs=[bad])).agreement == "caution"
    assert S.select_chain("t", SCAN, specs, evidence(cards, pairs=[few])).agreement == "caution"
    assert S.select_chain("t", SCAN, specs, evidence(cards)).agreement == "caution"  # never measured together
    assert S.select_chain("t", SCAN, [spec("a")], evidence([card("a")])).agreement == "off"  # one reader has nothing to compare with


# --- drift ---------------------------------------------------------------------------------------------------------------------

def test_an_engine_whose_live_accuracy_falls_below_its_scorecard_leaves_the_chain():
    specs = [spec("a"), spec("b")]
    cards = [card("a", 0.95, queued_accuracy=0.90), card("b", 0.85, queued_accuracy=0.80)]
    healthy = evidence(cards, live={"a": S.Live(checks=100, unchanged=88)})  # 0.88 vs 0.90: inside the margin
    assert S.select_chain("t", SCAN, specs, healthy).readers == ["a", "b"]
    drifted = evidence(cards, live={"a": S.Live(checks=100, unchanged=70)})  # 0.70 vs 0.90
    d = S.select_chain("t", SCAN, specs, drifted)
    assert d.readers == ["b"] and d.numbers["engines"]["a"]["excluded"] == "drifted"
    assert any("a left the chain" in n and "70/100" in n for n in d.notes)  # the numbers are in the decision


def test_too_few_live_checks_do_not_drift_an_engine_out():
    cards = [card("a", 0.95, queued_accuracy=0.90)]
    ev = evidence(cards, live={"a": S.Live(checks=config.ENGINE_DRIFT_MIN_CHECKS - 1, unchanged=0)})
    assert S.select_chain("t", SCAN, [spec("a")], ev).readers == ["a"]
    assert not drift.is_drifted(cards[0], S.Live(checks=0, unchanged=0))


def test_when_every_engine_has_drifted_the_template_falls_back_to_cold_start_review():
    cards = [card("a", 0.95, queued_accuracy=0.90)]
    d = S.select_chain("t", SCAN, [spec("a")], evidence(cards, live={"a": S.Live(checks=100, unchanged=10)}))
    assert d.mode == "cold_start" and d.force_review


# --- manual override -----------------------------------------------------------------------------------------------------------

def test_a_manual_chain_wins_but_unmeasured_engines_still_cannot_reach_auto():
    specs = [spec("a"), spec("b")]
    d = S.select_chain("t", SCAN, specs, evidence([card("a", 0.99)]), overrides={"t": ["b", "a"]})
    assert d.mode == "override" and d.readers == ["b", "a"] and d.chain == ["b", "a", "tesseract"]
    assert d.force_review  # b was never promoted
    d = S.select_chain("t", SCAN, specs, evidence([card("a", 0.99), card("b", 0.9)]), overrides={"t": ["b", "a"]})
    assert not d.force_review  # both were measured and promoted: the person chose the order, not the trust


def test_an_override_naming_nothing_usable_is_ignored_and_other_templates_are_untouched():
    specs = [spec("a")]
    ev = evidence([card("a")])
    assert S.select_chain("t", SCAN, specs, ev, overrides={"t": ["ghost"]}).mode == "measured"
    assert S.select_chain("other", SCAN, specs, ev, overrides={"t": ["a"]}).mode == "measured"


def test_overrides_are_off_by_default_and_a_bad_file_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ENGINE_CHAIN_OVERRIDES_FILE", "")
    assert S.load_overrides() == {}
    path = tmp_path / "o.json"
    path.write_text('{"t": "not a list"}', encoding="utf-8")
    monkeypatch.setattr(config, "ENGINE_CHAIN_OVERRIDES_FILE", str(path))
    assert S.load_overrides() == {}
    path.write_text('{"t": ["a"]}', encoding="utf-8")
    assert S.load_overrides() == {"t": ["a"]}


# --- promotion -----------------------------------------------------------------------------------------------------------------

def test_a_candidate_must_beat_tesseract_and_every_promoted_engine_on_the_same_corpus():
    cards = {"tesseract": card("tesseract", 0.20), "inc": card("inc", 0.90)}
    promoted = {"inc"}
    assert not promotion.judge(card("new", 0.905), cards, promoted).promote  # inside the margin
    v = promotion.judge(card("new", 0.93), cards, promoted)
    assert v.promote and v.numbers["incumbent"] == "inc" and v.numbers["incumbent_accuracy"] == 0.9


def test_a_candidate_is_compared_only_on_the_documents_the_incumbent_was_scored_on():
    cards = {"tesseract": card("tesseract", 0.20, corpus="other")}
    v = promotion.judge(card("new", 0.99), cards, set())
    assert not v.promote and "same documents" in v.reason


def test_a_candidate_that_would_ship_a_wrong_value_unchecked_or_has_too_little_data_is_not_promoted():
    cards = {"tesseract": card("tesseract", 0.2)}
    assert "auto" in promotion.judge(card("new", 0.99, wrong_auto=1), cards, set()).reason
    assert "fields" in promotion.judge(card("new", 0.99, fields=10), cards, set()).reason
    assert not promotion.judge(card("tesseract", 0.99), cards, set()).promote


# --- agreement -----------------------------------------------------------------------------------------------------------------

def dec(value, gated=False, valid=True):
    return Decision(value, 0 if value else None, valid, False, False, gated)


def test_disagreement_is_flagged_and_agreement_is_trusted_only_when_asked():
    a = {"x": dec("391.00"), "y": dec("EK"), "z": dec(""), "w": dec("12")}
    b = {"x": dec("391.0"), "y": dec(" ek "), "z": dec(""), "w": dec("12")}
    out, disagreed = AG.combine(a, b, trust=False)
    assert disagreed == {"x"} and not out["y"].gated and not out["w"].gated
    out, disagreed = AG.combine(a, b, trust=True)
    assert disagreed == {"x"} and out["y"].gated and out["w"].gated and not out["z"].gated  # a shared blank earns nothing
    out, _ = AG.combine({"v": dec("bad", valid=False)}, {"v": dec("bad", valid=False)}, trust=True)
    assert not out["v"].gated  # agreeing on a value that fails its format earns nothing


def test_a_disagreed_field_becomes_a_rejected_value_for_review():
    from app.modules.vlm_reader import reader as R
    from app.schemas.template import TemplateOut
    from tests.test_vlm_reader import field

    template = TemplateOut.model_construct(template_id="t", version=1, fields=[field("x")])
    out = R.to_results(template, {"x": dec("391.00", gated=True)}, frozenset({"x"}))
    assert out["x"].raw_text == "391.00" and out["x"].aggregate_confidence == 0.0  # the primary's reading, shown, but not accepted


# --- probe ---------------------------------------------------------------------------------------------------------------------

def _png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _page(blur=0):
    rng = np.random.default_rng(0)
    arr = np.full((1600, 1200), 255, np.uint8)
    for _ in range(300):  # text-like strokes
        y, x = rng.integers(20, 1580), rng.integers(20, 1100)
        arr[y:y + 3, x:x + int(rng.integers(20, 90))] = 0
    img = Image.fromarray(arr)
    return img.filter(ImageFilter.GaussianBlur(blur)) if blur else img


def test_the_probe_tells_a_crisp_page_from_a_blurred_one_and_reports_a_text_layer():
    crisp, blurred = probe.probe_page(_png(_page())), probe.probe_page(_png(_page(blur=6)))
    assert crisp.quality == "good" and blurred.quality in ("soft", "poor") and crisp.sharpness > blurred.sharpness
    assert not crisp.has_text_layer
    layer = TextLayer(words=(TextWord("Air", (0, 0, 0.1, 0.1)), TextWord("Waybill", (0.1, 0, 0.2, 0.1))))
    f = probe.probe_page(_png(_page()), layer)
    assert f.has_text_layer and f.script == "latin"


def test_the_probe_does_not_pretend_to_measure_handwriting_or_read_a_script_off_pixels():
    f = probe.probe_page(_png(_page()))
    assert f.handwriting_share is None and f.script is None
    assert probe.script_of(TextLayer(words=(TextWord("Груз", (0, 0, 1, 1)),))) == "cyrillic"
    assert probe.script_of(TextLayer(words=(TextWord("123", (0, 0, 1, 1)),))) is None
    assert probe.probe_page(b"not an image").quality == "unknown"


# --- the store -----------------------------------------------------------------------------------------------------------------

@pytest.fixture()
def tid():
    return f"sel_{uuid.uuid4().hex}"


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    path = tmp_path / "engines.json"
    path.write_text(json.dumps({"engines": [
        {"name": "general", "kind": "openai_compatible", "url": "http://g.test/v1", "model": "m"},
        {"name": "tuned", "kind": "service", "url": "http://t.test"}]}), encoding="utf-8")
    monkeypatch.setattr(config, "READER_ENGINES_FILE", str(path))
    monkeypatch.setattr(config, "READER_ENGINE", "")
    monkeypatch.setattr(config, "ENGINE_CHAIN_OVERRIDES_FILE", "")
    S.clear_cache()
    store._last_seen.clear()
    yield path
    S.clear_cache()
    store._last_seen.clear()


def test_scorecards_keep_history_and_the_latest_one_stands(tid):
    store.save_scorecard(tid, card("a", 0.80), 10, {})
    store.save_scorecard(tid, card("a", 0.90), 10, {})
    store.save_scorecard(tid, card("b", 0.70), 10, {})
    cards = store.latest_cards(tid)
    assert cards["a"].accuracy == 0.9 and cards["b"].accuracy == 0.7
    store.save_pair(tid, S.Pair("b", "a", "c1", 100, 60, 59))  # stored with one spelling whichever way it was given
    assert store.latest_pairs(tid) == [S.Pair("a", "b", "c1", 100, 60, 59)]


def test_promotion_is_recorded_and_can_be_withdrawn(tid):
    assert store.promoted_engines(tid) == set()
    store.promote(tid, "a", "c1", {"accuracy": 0.9})
    store.promote(tid, "a", "c2", {"accuracy": 0.92})  # promoting again updates, it does not duplicate
    assert store.promoted_engines(tid) == {"a"}
    assert store.demote(tid, "a") and not store.demote(tid, "a")
    assert store.promoted_engines(tid) == set()


def test_the_chain_is_cold_start_until_a_measured_engine_is_promoted_and_each_change_is_logged_with_its_numbers(tid, registry):
    d = S.select_chain_for(tid, SCAN)
    assert d.mode == "cold_start" and d.readers == ["tuned", "general"]
    log = store.chain_log(tid)
    assert [e["mode"] for e in log] == ["cold_start"] and log[0]["chain"] == ["cold_start", "tuned", "general"]

    S.select_chain_for(tid, TEXT)  # a different page of the same template, same engines: not a chain change
    assert len(store.chain_log(tid)) == 1

    store.save_scorecard(tid, card("general", 0.93, docs_per_min=1.0), 20, {})
    store.save_scorecard(tid, card("tuned", 0.89, docs_per_min=3.0), 20, {})
    store.save_scorecard(tid, card("tesseract", 0.20), 20, {})
    store.promote(tid, "general", "c1", {})  # new results arrived: the chain is re-ranked
    d = S.select_chain_for(tid, SCAN)
    assert d.mode == "measured" and d.readers == ["general"]  # `tuned` was measured but never promoted
    last = store.chain_log(tid)[0]
    assert last["mode"] == "measured" and last["chain"] == ["measured", "general"]
    assert last["numbers"]["engines"]["general"]["accuracy"] == 0.93 and last["numbers"]["engines"]["tuned"]["excluded"] == "not promoted"


def _settled(tid_, engine, value_read, value_confirmed, via="single"):
    """One review entry a person settled: what the engine read and what it ended up as."""
    with SessionLocal() as session:
        result = ExtractionResultRecord(template_id=tid_, template_version=1, extracted_at=datetime.now(timezone.utc), overall_status="needs_review",
                                        fields={"f": {"value": value_read, "confidence": 80.0, "needs_review": True, "approval_tier": "spot_check", "reasons": []}})
        session.add(result)
        session.commit()
        result_id = result.id
    qid = review_queue.enqueue_spot_check(result_id, "f", value_read, 80.0, "no_validator", reader="vlm", engine=engine)
    with SessionLocal() as session:
        row = session.get(ReviewQueueRecord, qid)
        review_queue.apply_confirmed_value(session, row, value_confirmed, via)
        session.commit()
    return result_id


def test_live_accuracy_counts_only_single_checks_of_the_engine_and_ignores_whitespace_and_case(tid):
    for i in range(6):
        _settled(tid, "a", "391.00", "391.00")
    _settled(tid, "a", "391.00", " 391.00 ")  # a retyped space is not an engine error
    _settled(tid, "a", "391.0", "391.00")  # a real correction
    _settled(tid, "a", "1", "2", via="bulk")  # a bulk accept is not an independent check
    _settled(tid, "b", "1", "2")  # another engine
    live = store.live_stats(tid, "a")
    assert (live.checks, live.unchanged) == (8, 7)
    assert store.live_stats(tid, "b") == S.Live(1, 0) and store.live_stats(tid, "zzz") == S.Live(0, 0)


def test_drift_found_from_real_review_entries_reranks_the_chain_and_logs_why(tid, registry, monkeypatch):
    monkeypatch.setattr(config, "ENGINE_DRIFT_MIN_CHECKS", 10)
    store.save_scorecard(tid, card("general", 0.95, queued_accuracy=0.90), 20, {})
    store.save_scorecard(tid, card("tuned", 0.90, queued_accuracy=0.85), 20, {})
    store.promote(tid, "general", "c1", {})
    store.promote(tid, "tuned", "c1", {})
    assert S.select_chain_for(tid, SCAN).readers == ["general", "tuned"]
    for _ in range(4):
        _settled(tid, "general", "wrong", "right")
    for _ in range(8):
        _settled(tid, "general", "right", "right")  # 8/12 unchanged = 0.67 against 0.90
    S.clear_cache()
    d = S.select_chain_for(tid, SCAN)
    assert d.readers == ["tuned"]
    last = store.chain_log(tid)[0]
    assert last["chain"] == ["measured", "tuned"] and last["numbers"]["engines"]["general"]["excluded"] == "drifted"
    assert last["numbers"]["engines"]["general"]["live"] == {"checks": 12, "accuracy": 0.6667}


# --- labels --------------------------------------------------------------------------------------------------------------------

def test_a_value_a_person_settles_is_saved_as_a_label_and_a_bulk_accept_is_not(tid):
    one = _settled(tid, "a", "391.0", "391.00")
    _settled(tid, "a", "1", "1", via="bulk")
    labels = store.labels_for(tid)
    assert labels == [{"result_id": one, "version": 1, "field_id": "f", "value": "391.00", "machine_value": "391.0", "engine": "a"}]


def test_a_correction_on_the_results_screen_is_saved_as_a_label_too(client, tid):
    with SessionLocal() as session:
        result = ExtractionResultRecord(template_id=tid, template_version=1, extracted_at=datetime.now(timezone.utc), overall_status="complete",
                                        fields={"f": {"field_id": "f", "value": "old", "confidence": 90.0, "needs_review": False, "approval_tier": "auto", "reasons": []}})
        session.add(result)
        session.commit()
        rid = result.id
    assert client.patch(f"/results/{rid}/fields/f", json={"value": "new"}).status_code == 200
    assert store.labels_for(tid) == [{"result_id": rid, "version": 1, "field_id": "f", "value": "new", "machine_value": "old", "engine": None}]
    assert client.patch(f"/results/{rid}/fields/f", json={"value": "newer"}).status_code == 200
    assert [l["value"] for l in store.labels_for(tid)] == ["newer"]  # a field settled twice keeps its latest value


# --- the pipeline --------------------------------------------------------------------------------------------------------------

def test_a_cold_start_decision_marks_the_page_so_nothing_on_it_can_be_auto(monkeypatch):
    from tests.test_vlm_reader import _all_fields_template, _generic, _stub_ocr
    from app.modules import pipeline
    from app.modules.vlm_reader import reader as R

    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    monkeypatch.setattr(config, "ENGINE_SELECTOR_ENABLED", True)
    _stub_ocr(monkeypatch)
    seen = {}

    def fake_select(template_id, features):
        seen["features"] = features
        return S.ChainDecision(template_id, ["tuned", "tesseract"], ["tuned"], "cold_start", force_review=True, agreement="off")

    monkeypatch.setattr(pipeline, "select_chain_for", fake_select)
    def fake_read_page(*a, **k):
        seen["kw"] = k
        return R.VlmPage(fields={}, alignment=None, gated=0, engine="tuned")

    monkeypatch.setattr(pipeline.vlm, "read_page", fake_read_page)
    result = pipeline.extract_page_fields(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), None)
    assert result.reader == "vlm" and result.force_review and result.engine == "tuned"
    assert seen["kw"]["engine_names"] == ["tuned"] and seen["kw"]["agreement"] == "off"
    assert seen["features"].has_text_layer is False


def test_selection_is_off_by_default_so_a_page_is_read_exactly_as_before(monkeypatch):
    from tests.test_vlm_reader import _all_fields_template, _generic, _stub_ocr
    from app.modules import pipeline
    from app.modules.vlm_reader import reader as R

    assert config.ENGINE_SELECTOR_ENABLED is False
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    _stub_ocr(monkeypatch)
    monkeypatch.setattr(pipeline, "select_chain_for", lambda *a, **k: pytest.fail("the selector must not run"))
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: R.VlmPage(fields={}, alignment=None, gated=0))
    result = pipeline.extract_page_fields(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), None)
    assert result.reader == "vlm" and not result.force_review


def test_a_selector_that_fails_does_not_stop_the_page_and_holds_it_at_spot_check(monkeypatch):
    from tests.test_vlm_reader import _all_fields_template, _generic, _stub_ocr
    from app.modules import pipeline
    from app.modules.vlm_reader import reader as R

    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    monkeypatch.setattr(config, "ENGINE_SELECTOR_ENABLED", True)
    _stub_ocr(monkeypatch)

    def broken(*a, **k):
        raise RuntimeError("database is down")

    monkeypatch.setattr(pipeline, "select_chain_for", broken)
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: R.VlmPage(fields={}, alignment=None, gated=0))
    result = pipeline.extract_page_fields(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), None)
    assert result.reader == "vlm" and result.force_review


def test_a_chain_with_no_model_engine_reads_with_tesseract_and_says_so(monkeypatch):
    from tests.test_vlm_reader import _all_fields_template, _generic, _stub_ocr
    from app.modules import pipeline

    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    monkeypatch.setattr(config, "ENGINE_SELECTOR_ENABLED", True)
    ocr = _stub_ocr(monkeypatch)
    monkeypatch.setattr(pipeline, "select_chain_for", lambda t, f: S.ChainDecision(t, ["tesseract"], [], "cold_start", True, "off"))
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: pytest.fail("no model engine is in the chain"))
    result = pipeline.extract_page_fields(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), None)
    assert result.reader == "ocr_fallback" and len(ocr) == 1 and result.force_review


# --- two engines reading one page ----------------------------------------------------------------------------------------------

class FakeEngine:
    configured = True
    prompts = None

    def __init__(self, name, answers=None, down=False):
        self.name, self.answers, self.down, self.asked = name, answers or {}, down, 0

    def read(self, items, max_new=512):
        from app.modules.vlm_reader.client import ReaderUnavailable
        self.asked += 1
        if self.down:
            raise ReaderUnavailable("down")
        return [self.answers.get(prompt_field, "EMPTY") for prompt_field in (p.split("|")[0] for _, p in items)]


def _two_engine_page(monkeypatch, engines):
    """read_page with the page work stubbed: two single-field crops (carrier, currency) and the given engines behind get_client."""
    from types import SimpleNamespace
    from app.modules.vlm_reader import reader as R
    from tests.test_vlm_reader import _all_fields_template, _generic

    monkeypatch.setattr(R, "preprocess_image", lambda *a, **k: _generic())
    monkeypatch.setattr(R.K, "page_tokens", lambda image: [])
    monkeypatch.setattr(R, "load_profile", lambda name: object())
    monkeypatch.setattr(R, "align_from_tokens", lambda template, tokens: SimpleNamespace(summary=lambda: "stub"))
    crops = [SimpleNamespace(png=lambda: b"png", kind="single", label=fid, captions="", hint="", fields=[fid], level=0) for fid in ("carrier", "currency")]
    monkeypatch.setattr(R.C, "crops_for_page", lambda *a, **k: crops)
    monkeypatch.setattr(R.K, "read_all", lambda *a, **k: {})
    monkeypatch.setattr(R.K, "printed_words", lambda profile: {})
    monkeypatch.setattr(R.W, "prompt_for", lambda kind, label, captions, hint, prompts=None: f"{label}|")
    monkeypatch.setattr(R.engines, "get_client", lambda name=None, transport=None: engines[name])
    return R, _all_fields_template(vlm_reader="waybill_B").model_copy(update={"landmarks": [object()]}), _generic()


def test_two_engines_that_disagree_send_that_field_to_review_and_the_primary_keeps_its_value(monkeypatch):
    a = FakeEngine("a", {"carrier": "EMIRATES", "currency": "USD"})
    b = FakeEngine("b", {"carrier": "EMIRATES", "currency": "EUR"})
    R, template, generic = _two_engine_page(monkeypatch, {"a": a, "b": b})
    page = R.read_page(template, b"", generic, engine_names=["a", "b"], agreement="caution")
    assert page.engine == "a" and page.engines == ("a", "b") and page.disagreed == {"currency"}
    assert page.fields["currency"].raw_text == "USD" and page.fields["currency"].aggregate_confidence == 0.0
    assert page.fields["carrier"].aggregate_confidence == config.VLM_UNGATED_CONFIDENCE  # agreed, but agreement is unmeasured: no promotion


def test_measured_agreement_gates_what_two_engines_both_read(monkeypatch):
    a = FakeEngine("a", {"carrier": "EMIRATES", "currency": "USD"})
    b = FakeEngine("b", {"carrier": "EMIRATES", "currency": "USD"})
    R, template, generic = _two_engine_page(monkeypatch, {"a": a, "b": b})
    page = R.read_page(template, b"", generic, engine_names=["a", "b"], agreement="trust")
    assert not page.disagreed and page.fields["currency"].aggregate_confidence == config.VLM_GATED_CONFIDENCE
    assert page.fields["currency"].aggregate_confidence < config.CONFIDENCE_AUTO_APPROVE_THRESHOLD * 100  # still a spot check, never auto


def test_a_chain_is_also_a_fallback_order_and_a_second_engine_is_only_asked_when_agreement_is_on(monkeypatch):
    down, b, c = FakeEngine("down", down=True), FakeEngine("b", {"currency": "USD"}), FakeEngine("c", {"currency": "USD"})
    R, template, generic = _two_engine_page(monkeypatch, {"down": down, "b": b, "c": c})
    page = R.read_page(template, b"", generic, engine_names=["down", "b", "c"], agreement="off")
    assert page.engines == ("b",) and c.asked == 0  # the first engine that works reads it; nobody else is paid for
    page = R.read_page(template, b"", generic, engine_names=["down", "b", "c"], agreement="caution")
    assert page.engines == ("b", "c")
    with pytest.raises(ReaderUnavailable):
        R.read_page(template, b"", generic, engine_names=["down"], agreement="off")


def test_an_engine_that_is_the_only_one_left_reads_alone_even_when_a_second_opinion_was_wanted(monkeypatch, caplog):
    only = FakeEngine("only", {"currency": "USD"})
    R, template, generic = _two_engine_page(monkeypatch, {"only": only, "down": FakeEngine("down", down=True)})
    with caplog.at_level("WARNING"):
        page = R.read_page(template, b"", generic, engine_names=["only", "down"], agreement="caution")
    assert page.engines == ("only",) and not page.disagreed and "second opinion" in caplog.text


def test_an_engines_own_prompt_wording_is_used_for_its_questions():
    from app.modules.vlm_reader import waybill as W
    assert "BESPOKE Carrier" in W.prompt_for("single", "Carrier", "", "", {"single": "BESPOKE {label}"})
    assert W.prompt_for("single", "Carrier", "caps", "h") == W.prompt_for("single", "Carrier", "caps", "h", {"party": "x"})  # a kind it omits keeps the shipped one


def test_a_prompt_file_is_loaded_per_engine_and_a_bad_one_makes_the_engine_unusable(tmp_path):
    from app.modules.vlm_reader.client import ReaderUnavailable
    good = tmp_path / "p.json"
    good.write_text(json.dumps({"single": "Read {label}"}), encoding="utf-8")
    assert E.EngineSpec(name="x", kind="service", url="http://x", prompt_file=str(good)).build().prompts == {"single": "Read {label}"}
    assert E.EngineSpec(name="x", kind="service", url="http://x").build().prompts is None
    for text in ("{not json", json.dumps({"surprise": "x"}), json.dumps({"single": 3})):
        good.write_text(text, encoding="utf-8")
        with pytest.raises(ReaderUnavailable):
            E.EngineSpec(name="x", kind="service", url="http://x", prompt_file=str(good)).build()
    with pytest.raises(ReaderUnavailable):
        E.EngineSpec(name="x", kind="service", url="http://x", prompt_file=str(tmp_path / "absent.json")).build()
