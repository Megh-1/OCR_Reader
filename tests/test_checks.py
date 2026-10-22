"""Cross-field checks: schema rules, the evaluator, and how Module 7 acts on a failure."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.modules.checks import evaluate_checks
from app.modules.field_extraction import FieldExtractionResult
from app.modules.validation import validate_fields
from app.schemas.template import TemplateCheck, TemplateIn, TemplateOut
from scripts.seed_templates import AIR_WAYBILL, AIR_WAYBILL_ROUTING


def check(**kw) -> TemplateCheck:
    base = dict(id="c", label="c", kind="equal", inputs=["a", "b"])
    return TemplateCheck(**{**base, **kw})


def run(chk: TemplateCheck, /, **values: str) -> str:
    return evaluate_checks([chk], values)[0].status


# --------------------------------------------------------------------------- schema


def test_each_kind_needs_its_own_shape():
    with pytest.raises(ValidationError, match="product needs exactly 2 inputs and a target"):
        check(kind="product", inputs=["a"], target="t")
    with pytest.raises(ValidationError, match="sum needs at least 2 inputs"):
        check(kind="sum", inputs=["a", "b"])
    with pytest.raises(ValidationError, match="equal needs exactly 2 inputs"):
        check(kind="equal", inputs=["a"])
    with pytest.raises(ValidationError, match="one_of needs"):
        check(kind="one_of", inputs=["a"])
    with pytest.raises(ValidationError, match="maps_to needs"):
        check(kind="maps_to", inputs=["a"], target="b")


@pytest.mark.parametrize("pattern", [r"^\d{3}", r"^(\d)(\d)", "(unclosed"])
def test_a_pattern_must_be_a_valid_regex_with_exactly_one_group(pattern):
    with pytest.raises(ValidationError):
        check(patterns={"a": pattern})


def _template(checks, field_ids=("a", "b", "t")):
    fields = [
        {"id": i, "label": i, "bbox": [0.1, 0.1, 0.2, 0.2], "data_type": "string", "confidence_floor": 0.6}
        for i in field_ids
    ]
    anchors = [{"text": "X", "bbox": [0.1, 0.1, 0.2, 0.2], "match_threshold": 0.7}]
    return TemplateIn(template_id="t", anchors=anchors, fields=fields, checks=checks)


def test_a_check_may_only_name_fields_the_template_has():
    with pytest.raises(ValidationError, match="does not have: zzz"):
        _template([check(inputs=["a", "zzz"])])
    with pytest.raises(ValidationError, match="does not have: nope"):
        _template([check(patterns={"nope": r"^(\d)"})])
    with pytest.raises(ValidationError, match="does not have: gate"):
        _template([check(when={"field": "gate", "equals": "Q"})])


def test_check_ids_must_be_unique_and_well_formed():
    with pytest.raises(ValidationError, match="duplicate check id"):
        _template([check(id="x"), check(id="x")])
    with pytest.raises(ValidationError, match="must match"):
        _template([check(id="Bad Id")])


def test_api_refuses_a_bad_check_and_stores_a_good_one(client, unique_doc_type):
    base = {
        "template_id": unique_doc_type,
        "anchors": [{"text": "X", "bbox": [0.1, 0.1, 0.2, 0.2], "match_threshold": 0.7}],
        "fields": [
            {"id": i, "label": i, "bbox": [0.1, 0.1, 0.2, 0.2], "data_type": "string", "confidence_floor": 0.6}
            for i in ("a", "b")
        ],
    }
    bad = {**base, "checks": [{"id": "c", "label": "c", "kind": "equal", "inputs": ["a", "ghost"]}]}
    assert client.post("/templates", json=bad).status_code == 422

    good = {**base, "checks": [{"id": "c", "label": "a = b", "kind": "equal", "inputs": ["a", "b"]}]}
    out = client.post("/templates", json=good)
    assert out.status_code == 201
    assert client.get(f"/templates/{unique_doc_type}").json()["checks"][0]["label"] == "a = b"


def test_a_template_stored_before_checks_existed_still_loads():
    old = AIR_WAYBILL.model_dump(mode="json")
    del old["checks"]
    out = TemplateOut(version=1, created_at=date.today(), **old)
    assert out.checks == []


# --------------------------------------------------------------------------- evaluator


def test_product_passes_fails_and_honours_tolerance():
    c = check(kind="product", inputs=["w", "r"], target="t", tolerance=0.01)
    assert run(c, w="65", r="170", t="11050") == "passed"
    assert run(c, w="65", r="170", t="11,050") == "passed"  # thousands separator
    assert run(c, w="65", r="170", t="11650") == "failed"
    assert run(c, w="65.5", r="170.25", t="11151.38") == "passed"  # 11151.375, rounded to the cent
    assert run(c, w="65.5", r="170.25", t="11151.50") == "failed"


def test_sum_adds_every_input():
    c = check(kind="sum", inputs=["a", "b", "c"], target="t")
    assert run(c, a="10", b="20.5", c="3", t="33.5") == "passed"
    assert run(c, a="10", b="20.5", c="3", t="33.6") == "failed"


@pytest.mark.parametrize(
    "values",
    [
        {"w": "65", "r": "abc", "t": "11050"},  # an input is not a number
        {"w": "", "r": "170", "t": "11050"},  # empty
        {"r": "170", "t": "11050"},  # missing
        {"w": "65", "r": "170", "t": "ELEVEN"},  # target is not a number
        {"w": "1.234,5", "r": "170", "t": "11050"},  # ambiguous European format is not guessed at
    ],
)
def test_a_check_it_cannot_decide_is_skipped_not_failed(values):
    assert run(check(kind="product", inputs=["w", "r"], target="t"), **values) == "skipped"


def test_when_gates_a_check_and_a_closed_gate_skips_it():
    c = check(kind="product", inputs=["w", "r"], target="t", when={"field": "cls", "equals": "Q"})
    assert run(c, w="65", r="170", t="1", cls="Q") == "failed"
    assert run(c, w="65", r="170", t="1", cls="M") == "skipped"  # minimum charge: not a plain product
    assert run(c, w="65", r="170", t="1", cls="q") == "failed"  # case-insensitive
    assert run(c, w="65", r="170", t="1") == "skipped"  # gate field missing


def test_equal_compares_the_captured_part_case_insensitively():
    c = check(kind="equal", inputs=["flight", "carrier"], patterns={"flight": r"^([A-Z0-9]{2})\d{1,4}/"})
    assert run(c, flight="EK0565/27FEB", carrier="EK") == "passed"
    assert run(c, flight="EK0565/27FEB", carrier="ek") == "passed"
    assert run(c, flight="EK0565/27FEB", carrier="QR") == "failed"
    assert run(c, flight="garbled", carrier="EK") == "skipped"  # pattern does not match


def test_one_of_uses_values_or_the_iso_list():
    iso = check(kind="one_of", inputs=["cur"], lookup="iso4217")
    assert run(iso, cur="INR") == "passed"
    assert run(iso, cur="INF") == "failed"  # a one-letter misread of INR that a 3-letter regex accepts
    assert run(iso, cur="") == "skipped"
    own = check(kind="one_of", inputs=["code"], values=["PP", "CC"])
    assert run(own, code="pp") == "passed"
    assert run(own, code="XX") == "failed"


def test_maps_to_skips_a_key_it_does_not_know():
    c = check(kind="maps_to", inputs=["awb"], target="carrier", patterns={"awb": r"^(\d{3})"}, table={"176": "EMIRATES"})
    assert run(c, awb="176-06067736", carrier="EMIRATES") == "passed"
    assert run(c, awb="176-06067736", carrier="QATAR") == "failed"
    assert run(c, awb="157-12345678", carrier="QATAR") == "skipped"  # another airline: no opinion
    assert run(c, awb="176-06067736", carrier="") == "skipped"


# --------------------------------------------------------------------------- module 7


REFERENCE = {
    "awb_number": "176-06067736", "carrier": "EMIRATES", "first_carrier_code": "EK",
    "flight_date": "EK0565/27FEB", "currency": "INR", "chargeable_weight": "65",
    "rate_charge": "170", "rate_class": "Q", "total_charge": "11050",
}


def _template_out(template: TemplateIn) -> TemplateOut:
    return TemplateOut(version=1, created_at=date.today(), **template.model_dump(mode="json"))


def _extractions(template: TemplateOut, **overrides: str) -> dict[str, FieldExtractionResult]:
    values = {**REFERENCE, **overrides}
    return {
        f.id: FieldExtractionResult(f.id, values.get(f.id, ""), [100.0], 100.0, 0, "text_layer") for f in template.fields
    }


def _check_reasons(summary):
    return {k: [r for r in v.reasons if r.startswith("cross_check_failed")] for k, v in summary.flagged.items()}


def test_the_reference_values_pass_every_waybill_check():
    template = _template_out(AIR_WAYBILL)
    summary = validate_fields(template, _extractions(template))
    assert {o.check_id: o.status for o in summary.checks} == {
        "total_equals_weight_times_rate": "passed",
        "flight_prefix_matches_first_carrier": "passed",
        "currency_is_iso_4217": "passed",
        "awb_prefix_matches_carrier": "passed",
    }
    assert not any(_check_reasons(summary).values())


def test_a_misread_total_flags_all_three_fields_that_make_up_the_product():
    template = _template_out(AIR_WAYBILL)
    summary = validate_fields(template, _extractions(template, total_charge="11650"))
    named = {k: v for k, v in _check_reasons(summary).items() if v}
    assert set(named) == {"chargeable_weight", "rate_charge", "total_charge"}
    reason = named["total_charge"][0]
    assert reason == "cross_check_failed:Total charge = chargeable weight x rate"
    # each field passed its OWN checks (it is a plausible number), yet is now review
    assert summary.flagged["total_charge"].approval_tier == "review"
    assert summary.flagged["total_charge"].accepted is False


def test_a_minimum_charge_is_not_flagged_by_the_product_check():
    template = _template_out(AIR_WAYBILL)
    summary = validate_fields(template, _extractions(template, rate_class="M", total_charge="500"))
    product = next(o for o in summary.checks if o.check_id == "total_equals_weight_times_rate")
    assert product.status == "skipped"


def test_a_misread_currency_is_caught_even_though_the_regex_accepts_it():
    template = _template_out(AIR_WAYBILL)
    summary = validate_fields(template, _extractions(template, currency="INF"))
    assert summary.flagged["currency"].reasons == ["cross_check_failed:Currency is a real ISO 4217 code"]


def test_a_failed_check_adds_a_reason_to_an_already_flagged_field_and_never_removes_one():
    template = _template_out(AIR_WAYBILL)
    ex = _extractions(template, currency="INF")
    ex["currency"] = FieldExtractionResult("currency", "INF", [10.0], 10.0, 7, "ocr")  # also under its floor
    reasons = validate_fields(template, ex).flagged["currency"].reasons
    assert "low_confidence" in reasons
    assert "cross_check_failed:Currency is a real ISO 4217 code" in reasons
    assert len(reasons) == len(set(reasons))


def test_a_passing_or_skipped_check_changes_nothing():
    """Cross-checks may only add caution: with them removed, the partition is identical."""
    with_checks = _template_out(AIR_WAYBILL)
    without = with_checks.model_copy(update={"checks": []})
    a = validate_fields(with_checks, _extractions(with_checks))
    b = validate_fields(without, _extractions(without))
    assert {k: v.approval_tier for k, v in a.accepted.items()} == {k: v.approval_tier for k, v in b.accepted.items()}
    assert a.flagged.keys() == b.flagged.keys()


def test_the_routing_template_keeps_only_the_checks_it_can_evaluate():
    assert [c.id for c in AIR_WAYBILL_ROUTING.checks] == ["flight_prefix_matches_first_carrier", "currency_is_iso_4217"]
