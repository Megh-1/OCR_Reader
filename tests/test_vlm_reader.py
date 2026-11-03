"""The vision-model reader (app/modules/vlm_reader): the decision rules, the service client, the mapping into Module 6's results and
the pipeline's fall-back. No model and no GPU: the service is a stub. The page-for-page replay against the saved GPU replies is
tests/test_vlm_reader_replay.py (needs the untracked corpus)."""

import base64
import json
import sys
from datetime import date
from pathlib import Path

import httpx
import pytest
from PIL import Image

import app.modules.pipeline as pipeline
from app import config
from app.modules.field_extraction import FieldExtractionResult
from app.modules.preprocessing import ProcessedImage
from app.modules.validation import validate_field
from app.modules.vlm_reader import decide as D
from app.modules.vlm_reader import reader as R
from app.modules.vlm_reader import waybill as W
from app.modules.vlm_reader.client import ReaderClient, ReaderUnavailable
from app.schemas.template import TemplateField, TemplateOut

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts" / "gpu"))
from scripts import waybill_forms as wf  # noqa: E402


def field(id, regex=None, **kw):
    base = dict(id=id, label=id, bbox=(0.1, 0.1, 0.5, 0.2), data_type="string", confidence_floor=0.6, validation_regex=regex)
    base.update(kw)
    return TemplateField(**base)


NUMBER = field("n", wf.NUMBER)


# --- the constants the app repeats must equal the scripts' --------------------------------------------------------------


def test_waybill_patterns_equal_the_forms_module():
    assert W.NUMBER == wf.NUMBER and W.AWB_FROM_BOX == wf.AWB_FROM_BOX and W.DECLARED_VALUE == wf.DECLARED_VALUE
    assert W.MONTH == wf._MONTH and W.DAY == wf._DAY and W.MONTH_NUMBER == wf._MONTH_NUMBER and W.DESIGNATOR == wf.DESIGNATOR
    assert W.FLIGHT == wf.FLIGHT and W.DESCRIPTION == wf.DESCRIPTION and W.HS_CODE == wf.HS_CODE and W.INVOICE == wf.INVOICE


def test_the_prompts_equal_the_trainers():
    import prompts  # scripts/gpu/prompts.py: what the adapter was trained with
    assert W.SINGLE_PROMPT == prompts.SINGLE and W.PARTY_PROMPT == prompts.PARTY and W.GOODS_PROMPT == prompts.GOODS
    entry = {"kind": "single", "label": "Currency", "captions": "Currency", "hint": W.HINTS["awb_number"]}
    assert W.prompt_for("single", "Currency", "Currency", W.HINTS["awb_number"]) == prompts.prompt_for(entry)


def test_every_template_field_set_member_has_a_zone():
    assert W.FIELD_IDS == set(wf.AIR_WAYBILL_BKK.fields and {f.id for f in wf.AIR_WAYBILL_BKK.fields})


# --- staged answers -----------------------------------------------------------------------------------------------------


def test_the_tightest_window_that_passes_its_format_wins():
    assert D.staged(NUMBER, {0: "1,432.00", 1: "9,999.00", 2: "9,999.00"}) == ("1,432.00", 0, True)


def test_widening_is_used_only_when_the_tight_window_gave_nothing_usable():
    assert D.staged(NUMBER, {0: "", 1: "500.00", 2: "500.00"}) == ("500.00", 1, True)
    assert D.staged(NUMBER, {0: "ABC", 1: "500.00", 2: ""}) == ("500.00", 1, True)


def test_a_wider_answer_that_contains_the_chosen_one_replaces_a_clipped_read():
    # the tight window cut the last digit; the model copied what it saw and the clipped value still passed its format
    assert D.staged(NUMBER, {0: "1432.0", 1: "1432.00", 2: "1432.00"}) == ("1432.00", 1, True)


def test_a_blank_stays_blank_when_the_wider_windows_read_different_neighbours():
    assert D.staged(NUMBER, {0: "", 1: "7,430.00", 2: "500.00"}) == ("", None, False)
    # ... but the same value found twice by widening is kept
    assert D.staged(NUMBER, {0: "", 1: "500.00", 2: "500.00"})[0] == "500.00"


def test_an_answer_that_fails_the_format_is_kept_but_flagged_as_invalid():
    assert D.staged(NUMBER, {0: "12,3", 1: "", 2: ""}) == ("12,3", 0, False)


def test_a_declared_selection_is_applied_before_the_format_is_checked():
    awb = field("awb", wf.AWB, squash_spaces=True, extract_pattern=r"(\d{3}\s*-?\s*\d{4}\s*\d{4})")
    assert D.staged(awb, {0: "176-0669 1322"}) == ("176-06691322", 0, True)


def test_reply_parsing():
    assert D.value_of("EMPTY") == "" and D.value_of(" `12.00` ") == "12.00" and D.value_of('"FREIGHT PREPAID"') == '"FREIGHT PREPAID"'
    assert D.answer_fields("party", ["n", "a"], 'x {"name": "ACME", "address": "1 Main St"} y') == {"n": "ACME", "a": "1 Main St"}
    assert D.answer_fields("goods", [], '{"description": "SHOES", "hs_code": "EMPTY", "invoice_number": "INV 5"}') == {
        "nature_of_goods": "SHOES", "hs_code": "", "invoice_reference": "INV 5"}
    assert D.answer_fields("party", ["n", "a"], "not json") == {"n": "", "a": ""}


# --- the gate -----------------------------------------------------------------------------------------------------------


def test_the_awb_check_digit_and_the_arithmetic_corroborate_only_what_they_cover():
    assert D.checked_fields({"awb_number": "176-06067736"}) == {"awb_number"}  # 0606773 mod 7 = 6
    assert D.checked_fields({"awb_number": "176-06067737"}) == set()
    assert D.checked_fields({"chargeable_weight": "10.00", "rate_charge": "2.50", "total_charge": "25.00"}) == {"chargeable_weight", "rate_charge", "total_charge"}
    assert D.checked_fields({"chargeable_weight": "10.00", "rate_charge": "2.50", "total_charge": "25.10"}) == set()
    sums = {"prepaid_weight_charge": "100.00", "total_other_charges_agent": "5.00", "total_other_charges_carrier": "", "total_prepaid": "105.00"}
    assert D.checked_fields(sums) == {"prepaid_weight_charge", "total_prepaid", "total_other_charges_agent"}


def test_a_value_is_gated_when_the_word_reader_agrees_or_an_arithmetic_check_holds_and_the_format_passes():
    fields = {"a": NUMBER, "b": field("b", wf.NUMBER), "c": field("c", wf.NUMBER)}
    answers = {"a": {0: "12.00"}, "b": {0: "13.00"}, "c": {0: "x1"}}
    d = D.decide(fields, answers, {"a": "12.00", "b": "99.00", "c": "x1"})
    assert d["a"].gated and d["a"].agree
    assert not d["b"].gated  # the readers disagree
    assert not d["c"].gated and d["c"].agree  # they agree on text that fails the format: never gated


def test_window_agreement_alone_is_not_a_gate():
    d = D.decide({"a": NUMBER}, {"a": {0: "12.00", 1: "12.00", 2: "12.00"}}, {"a": ""})
    assert not d["a"].gated


def test_a_field_with_no_answer_is_blank_and_never_gated():
    d = D.decide({"a": NUMBER}, {}, {"a": ""})
    assert d["a"].value == "" and not d["a"].gated and d["a"].level is None


# --- mapping into Module 6's results, and from there into tiers -----------------------------------------------------------


def _template(fields, **hints):
    return TemplateOut(template_id="t", version=1, anchors=[], fields=fields, created_at=date.today(), hints=hints)


def test_confidence_is_fixed_by_what_was_established():
    template = _template([NUMBER, field("opt", optional=True, from_field="n", extract_pattern=r"(\d+)"), field("other", wf.NUMBER)])
    decisions = {
        "n": D.Decision("1.00", 0, True, True, False, True),
        "opt": D.Decision("", None, False, False, False, False),
        "other": D.Decision("", None, False, False, False, False),
    }
    out = R.to_results(template, decisions)
    assert out["n"].aggregate_confidence == config.VLM_GATED_CONFIDENCE and out["n"].source == "vlm"
    assert out["opt"].aggregate_confidence == config.VLM_UNGATED_CONFIDENCE  # a blank optional field is a legitimate answer
    assert out["other"].aggregate_confidence == 0.0  # a blank required field is rejected


def test_a_gated_value_is_a_spot_check_by_default_and_auto_only_when_the_operator_says_so(monkeypatch):
    read = lambda c: FieldExtractionResult("n", "1.00", [], c, 0, "vlm")  # noqa: E731
    assert config.VLM_GATED_CONFIDENCE < config.CONFIDENCE_AUTO_APPROVE_THRESHOLD * 100
    assert validate_field(NUMBER, read(config.VLM_GATED_CONFIDENCE)).approval_tier == "spot_check"
    assert validate_field(NUMBER, read(config.VLM_UNGATED_CONFIDENCE)).approval_tier == "spot_check"
    assert validate_field(NUMBER, read(0.0)).approval_tier == "review"
    assert validate_field(NUMBER, read(95.0)).approval_tier == "auto"  # VLM_GATED_CONFIDENCE=95 would do this


# --- the client ---------------------------------------------------------------------------------------------------------


def _png() -> bytes:
    import io
    buf = io.BytesIO()
    Image.new("L", (20, 10), 255).save(buf, format="PNG")
    return buf.getvalue()


def _client(handler, **kw):
    return ReaderClient(url="http://reader.test", token="s3cret", transport=httpx.MockTransport(handler), **kw)


def test_the_client_sends_crops_in_chunks_with_the_token_and_returns_replies_in_order():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((request.headers["authorization"], len(body["requests"]), body["max_new"]))
        assert base64.b64decode(body["requests"][0]["image_b64"]) == _png()
        return httpx.Response(200, json={"replies": [r["prompt"].upper() for r in body["requests"]]})

    items = [(_png(), f"q{i}") for i in range(5)]
    assert _client(handler, per_request=2).read(items, max_new=64) == ["Q0", "Q1", "Q2", "Q3", "Q4"]
    assert seen == [("Bearer s3cret", 2, 64), ("Bearer s3cret", 2, 64), ("Bearer s3cret", 1, 64)]


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(401, json={"detail": "bad"}),
    httpx.Response(200, json={"replies": ["only one"]}),  # short
    httpx.Response(200, json={"nope": 1}),
    httpx.Response(200, text="not json"),
])
def test_any_bad_answer_is_reader_unavailable(response):
    with pytest.raises(ReaderUnavailable):
        _client(lambda request: response).read([(_png(), "a"), (_png(), "b")])


def test_an_unreachable_service_and_an_unset_url_are_reader_unavailable():
    def refuse(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ReaderUnavailable):
        _client(refuse).read([(_png(), "a")])
    with pytest.raises(ReaderUnavailable, match="not set"):
        ReaderClient(url="").read([(_png(), "a")])


def test_connecting_has_its_own_short_limit_but_a_reply_may_take_the_whole_timeout():
    """A tunnel that died hangs the connection instead of refusing it; waiting out the reply timeout for each page was measured at 130 s."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.extensions["timeout"])
        return httpx.Response(200, json={"replies": ["x"]})

    _client(handler, timeout=120).read([(_png(), "a")])
    assert seen["connect"] == config.VLM_READER_CONNECT_TIMEOUT_S < seen["read"] == 120


# --- the pipeline's choice of reader --------------------------------------------------------------------------------------


def _all_fields_template(**hints):
    fields = [field(fid) for fid in sorted(W.FIELD_IDS)]
    return _template(fields, **hints)


def _generic() -> ProcessedImage:
    import io
    buf = io.BytesIO()
    Image.new("L", (40, 60), 255).save(buf, format="PNG")
    png = buf.getvalue()
    return ProcessedImage(image_bytes=png, field_image_bytes=png, skew_angle_corrected=0.0, osd_fallback_used=False, final_dpi=200.0, final_size=(40, 60))


def _stub_ocr(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "preprocess_for_template", lambda page_bytes, template, generic: generic)
    monkeypatch.setattr(pipeline, "extract_fields", lambda *a, **k: calls.append(a) or {"stub": "tesseract"})
    return calls


def test_a_template_without_a_reader_profile_is_read_by_tesseract_whatever_the_service_says(monkeypatch):
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    ocr = _stub_ocr(monkeypatch)
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: pytest.fail("the vision reader must not run"))
    result = pipeline.extract_page_fields(_all_fields_template(), b"", _generic(), None)
    assert result.reader == "ocr" and len(ocr) == 1


def test_the_reader_is_off_until_the_service_url_is_set(monkeypatch):
    monkeypatch.setattr(config, "VLM_READER_URL", "")
    ocr = _stub_ocr(monkeypatch)
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: pytest.fail("the vision reader must not run"))
    assert pipeline.extract_page_fields(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), None).reader == "ocr"
    assert len(ocr) == 1


def test_a_page_the_vision_reader_cannot_read_falls_back_to_tesseract_and_says_so(monkeypatch, caplog):
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    ocr = _stub_ocr(monkeypatch)

    def unavailable(*a, **k):
        raise ReaderUnavailable("the service is down")

    monkeypatch.setattr(pipeline.vlm, "read_page", unavailable)
    with caplog.at_level("WARNING"):
        result = pipeline.extract_page_fields(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), None)
    assert result.reader == "ocr_fallback" and len(ocr) == 1 and result.fields == {"stub": "tesseract"}
    assert "the service is down" in caplog.text


def test_a_page_the_vision_reader_reads_runs_no_tesseract(monkeypatch):
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    ocr = _stub_ocr(monkeypatch)
    page = R.VlmPage(fields={"x": FieldExtractionResult("x", "1", [], 70.0, 0, "vlm")}, alignment=None, gated=0)
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: page)
    result = pipeline.extract_page_fields(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), None)
    assert result.reader == "vlm" and not ocr and result.fields["x"].source == "vlm"


def _text_read(template, bad=()):
    """What the text layer would have read: every field present and confident, except those named in `bad` (a blank, so rejected)."""
    return {f.id: FieldExtractionResult(f.id, "" if f.id in bad else "x", [], 0.0 if f.id in bad else 100.0, 0, "text_layer") for f in template.fields}


def test_a_text_native_page_whose_read_is_clean_stays_on_the_exact_text_layer(monkeypatch):
    from app.models.text_layer import TextLayer
    template = _all_fields_template(vlm_reader="waybill_B")
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: pytest.fail("nothing was rejected; the exact read stands"))
    monkeypatch.setattr(pipeline, "extract_fields_from_text_layer", lambda *a, **k: _text_read(template))
    result = pipeline.extract_page_fields(template, b"", _generic(), TextLayer(words=()))
    assert result.reader == "text_layer" and result.text_native


def test_a_text_native_page_whose_read_was_rejected_goes_to_the_vision_reader_with_its_words(monkeypatch):
    from app.models.text_layer import TextLayer
    template = _all_fields_template(vlm_reader="waybill_B")
    layer = TextLayer(words=())
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    monkeypatch.setattr(pipeline, "extract_fields_from_text_layer", lambda *a, **k: _text_read(template, bad={"awb_number"}))
    seen = {}
    page = R.VlmPage(fields={"x": FieldExtractionResult("x", "1", [], 70.0, 0, "vlm")}, alignment=None, gated=0)
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: seen.update(k) or page)
    result = pipeline.extract_page_fields(template, b"", _generic(), layer)
    assert result.reader == "vlm" and not result.text_native and seen["text_layer"] is layer


def test_a_rejected_text_native_page_keeps_its_text_layer_when_the_reader_is_unavailable(monkeypatch, caplog):
    from app.models.text_layer import TextLayer
    template = _all_fields_template(vlm_reader="waybill_B")
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    read = _text_read(template, bad={"awb_number"})
    monkeypatch.setattr(pipeline, "extract_fields_from_text_layer", lambda *a, **k: read)

    def unavailable(*a, **k):
        raise ReaderUnavailable("the service is down")

    monkeypatch.setattr(pipeline.vlm, "read_page", unavailable)
    with caplog.at_level("WARNING"):
        result = pipeline.extract_page_fields(template, b"", _generic(), TextLayer(words=()))
    assert result.reader == "text_layer" and result.text_native and result.fields is read  # not Tesseract: the layer is the better fallback
    assert "the service is down" in caplog.text


def test_a_template_without_a_reader_keeps_every_text_native_page_on_the_text_layer(monkeypatch):
    from app.models.text_layer import TextLayer
    template = _all_fields_template()
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    monkeypatch.setattr(pipeline.vlm, "read_page", lambda *a, **k: pytest.fail("the template names no reader"))
    monkeypatch.setattr(pipeline, "extract_fields_from_text_layer", lambda *a, **k: _text_read(template, bad={"awb_number"}))
    assert pipeline.extract_page_fields(template, b"", _generic(), TextLayer(words=())).reader == "text_layer"


def test_the_layers_words_become_exact_word_reader_tokens():
    from app.models.text_layer import TextLayer, TextWord
    layer = TextLayer(words=(TextWord("176-06067736", (0.7, 0.02, 0.8, 0.03)), TextWord("  ", (0, 0, 0.1, 0.1))))
    assert R.layer_tokens(layer) == [("176-06067736", 0.7, 0.02, 0.8, 0.03, 100.0)]


def test_read_page_refuses_what_it_cannot_do_before_calling_the_service(monkeypatch):
    called = []
    client = _client(lambda request: called.append(request) or httpx.Response(200, json={"replies": []}))
    with pytest.raises(ReaderUnavailable, match="lacks waybill fields"):
        R.read_page(_template([NUMBER], vlm_reader="waybill_B"), b"", _generic(), client)
    with pytest.raises(ReaderUnavailable, match="no landmarks"):
        R.read_page(_all_fields_template(vlm_reader="waybill_B"), b"", _generic(), client)
    with pytest.raises(ReaderUnavailable, match="names no reader profile"):
        R.read_page(_all_fields_template(), b"", _generic(), client)
    assert not called


def _reader_up_to_alignment(monkeypatch):
    """read_page with everything before the alignment stubbed: the page yields no words, so the reader's own refit fails."""
    monkeypatch.setattr(R, "preprocess_image", lambda *a, **k: _generic())
    monkeypatch.setattr(R.K, "page_tokens", lambda image: [])
    monkeypatch.setattr(R, "load_profile", lambda name: object())
    monkeypatch.setattr(R, "align_from_tokens", lambda template, tokens: None)

    def reached_crops(*a, **k):
        raise RuntimeError("reached the crops")

    monkeypatch.setattr(R.C, "crops_for_page", reached_crops)
    return _all_fields_template(vlm_reader="waybill_B").model_copy(update={"landmarks": [object()]})


def test_a_page_whose_own_refit_finds_no_words_is_cut_with_the_alignment_module_5_verified(monkeypatch):
    template = _reader_up_to_alignment(monkeypatch)
    client = _client(lambda request: httpx.Response(200, json={"replies": []}))
    with pytest.raises(ReaderUnavailable, match="could not be aligned"):
        R.read_page(template, b"", _generic(), client)
    with pytest.raises(RuntimeError, match="reached the crops"):  # past the alignment step, using the verified one
        R.read_page(template, b"", _generic(), client, verified=object())


def test_the_seeded_waybill_templates_name_their_profiles_and_the_partial_one_does_not():
    from scripts import seed_templates as S
    assert wf.AIR_WAYBILL_BKK.hints.vlm_reader == "waybill_B"
    assert S.AIR_WAYBILL.hints.vlm_reader == "waybill_M"
    assert S.AIR_WAYBILL_ROUTING.hints.vlm_reader is None
    assert W.FIELD_IDS <= {f.id for f in wf.AIR_WAYBILL_BKK.fields} and W.FIELD_IDS <= {f.id for f in S.AIR_WAYBILL.fields}
