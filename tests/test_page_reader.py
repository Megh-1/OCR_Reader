"""The whole-page reader (app/modules/page_reader.py) against a mocked OpenAI-compatible engine: valid, invalid, partial and failing replies."""

import io
import json

import httpx
from PIL import Image

from app.modules import page_reader as pr
from app.modules.vlm_reader.engines import EngineSpec
from scripts.seed_templates import AIR_WAYBILL

SPEC = EngineSpec(name="page_test", kind="openai_compatible", role="page", url="http://engine.invalid/v1", model="m", timeout_s=5)


def _png(size=(800, 1100)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, "PNG")
    return buf.getvalue()


def _reply(content: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        handler.calls.append(json.loads(request.content))
        return httpx.Response(status, json={"choices": [{"message": {"content": content}}]})
    handler.calls = []
    return handler


def _read(handler, **kw):
    return pr.read_page(_png(), SPEC, transport=httpx.MockTransport(handler), **kw)


def _full(values: dict[str, str]) -> str:
    return json.dumps({fid: {"value": values.get(fid, ""), "source_text": values.get(fid, "")} for fid in pr.FIELD_IDS})


def test_field_definitions_cover_exactly_the_template_fields():
    assert pr.FIELD_IDS == [f.id for f in AIR_WAYBILL.fields]
    assert len(pr.FIELD_IDS) == 33


def test_a_valid_reply_is_parsed_per_field_with_its_source_text():
    h = _reply(_full({"awb_number": "176-06067736", "carrier": "EMIRATES"}))
    r = _read(h)
    assert r.ok and r.reason is None and r.attempts == 1
    assert r.fields["awb_number"].value == "176-06067736" and r.fields["awb_number"].source_text == "176-06067736"
    assert r.fields["shipper_name"].value == ""
    body = h.calls[0]
    assert body["temperature"] == 0 and body["response_format"]["type"] == "json_schema"
    assert body["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_the_prompt_names_every_field_and_carries_no_document_value():
    prompt = pr.build_prompt()
    assert all(f'"{fid}"' in prompt for fid in pr.FIELD_IDS)
    assert "<embedded_text>" not in prompt
    assert "<embedded_text>" in pr.build_prompt(layer_text="ANY WORDS")


def test_a_partial_reply_fills_the_missing_fields_with_blanks():
    r = _read(_reply(json.dumps({"awb_number": {"value": "123-45678901", "source_text": "x"}})))
    assert r.ok and r.fields["awb_number"].value == "123-45678901" and r.fields["total_prepaid"].value == ""
    assert set(r.fields) == set(pr.FIELD_IDS)


def test_a_reply_in_a_code_fence_or_with_plain_string_cells_still_parses():
    fenced = "```json\n" + json.dumps({"carrier": "SOME AIRLINE"}) + "\n```"
    assert _read(_reply(fenced)).fields["carrier"].value == "SOME AIRLINE"


def test_invalid_json_is_retried_once_then_every_field_is_blank_with_reader_failed():
    h = _reply("I cannot read this page")
    r = _read(h)
    assert not r.ok and r.reason == "reader_failed" and r.attempts == 2 and len(h.calls) == 2
    assert all(f.value == "" for f in r.fields.values())


def test_the_second_attempt_can_rescue_an_invalid_first_reply():
    replies = iter(["not json", _full({"carrier": "X AIR"})])

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    r = _read(handler)
    assert r.ok and r.attempts == 2 and r.fields["carrier"].value == "X AIR"


def test_a_timeout_or_server_error_never_raises():
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    r = _read(boom)
    assert not r.ok and r.reason == "reader_failed" and r.attempts == 2
    assert not _read(_reply("{}", status=500)).ok


def test_an_undecodable_image_is_a_failed_page_not_a_crash():
    r = pr.read_page(b"not an image", SPEC, transport=httpx.MockTransport(_reply(_full({}))))
    assert not r.ok and r.reason == "reader_failed"


def test_the_page_is_scaled_down_never_up():
    _b64, size = pr.encode_image(_png((1000, 4000)), 2000)
    assert max(size) == 2000
    _b64, size = pr.encode_image(_png((500, 700)), 2000)
    assert size == (500, 700)


def test_normaliser_repairs_by_type_and_leaves_the_rest():
    assert pr.normalise_value("awb_number", "205-6129 0482") == "205-61290482"
    assert pr.normalise_value("awb_number", "20561290482") == "205-61290482"
    assert pr.normalise_value("total_prepaid", "237.390.00") == "237,390.00"
    assert pr.normalise_value("gross_weight", "AS 500.00") == "500.00"
    assert pr.normalise_value("total_charge", "1,234.00") == "1,234.00"
    assert pr.normalise_value("shipper_name", "AS 500.00") == "AS 500.00"
    assert pr.normalise_value("awb_number", "garbage") == "garbage"


def test_halves_reads_each_part_and_a_part_value_replaces_the_whole_page_one():
    top, bottom = pr.TOP_FIELDS, pr.BOTTOM_FIELDS
    replies = iter([_full({"carrier": "WHOLE", "total_prepaid": "1.00"}), _full({"carrier": "UPPER"}), _full({"total_prepaid": ""})])

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    r = _read(handler, halves=True)
    assert r.fields["carrier"].value == "UPPER"  # the part's own non-empty value wins
    assert r.fields["total_prepaid"].value == "1.00"  # an empty part value leaves the whole-page one
    assert top and bottom and set(top) | set(bottom) == set(pr.FIELD_IDS)
