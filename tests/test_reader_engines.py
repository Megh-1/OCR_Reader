"""The reader-engine registry and the OpenAI-compatible crop reader: swapping the model that reads a page is configuration, and
nothing the registry cannot use is a crash - it is `ReaderUnavailable`, which the pipeline answers by reading the page the ordinary way."""

import base64
import json

import httpx
import pytest

from app import config
from app.modules.vlm_reader import engines
from app.modules.vlm_reader.client import ReaderClient, ReaderUnavailable
from app.modules.vlm_reader.openai_client import OpenAICompatibleClient

PNG = b"\x89PNG\r\n\x1a\nfake"


def _client(handler, **kw) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(url="http://engine.test/v1/", model="some/model", token="s3cret", name="cand",
                                  transport=httpx.MockTransport(handler), **kw)


def _ok(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def test_request_carries_the_model_the_image_the_question_and_the_token():
    seen = {}

    def handler(request):
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json=_ok("391.00"))

    assert _client(handler).read([(PNG, "What is the total?")], max_new=64) == ["391.00"]
    body = seen["body"]
    assert seen["url"] == "http://engine.test/v1/chat/completions" and seen["auth"] == "Bearer s3cret"
    assert body["model"] == "some/model" and body["max_tokens"] == 64 and body["temperature"] == 0
    parts = body["messages"][0]["content"]
    assert parts[0]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert parts[1] == {"type": "text", "text": "What is the total?"}


def test_replies_come_back_in_the_order_of_the_crops_even_when_answered_out_of_order():
    def handler(request):
        question = json.loads(request.content)["messages"][0]["content"][1]["text"]
        return httpx.Response(200, json=_ok(f"answer {question}"))

    items = [(PNG, str(i)) for i in range(20)]
    assert _client(handler, concurrency=8).read(items) == [f"answer {i}" for i in range(20)]


def test_max_tokens_cap_and_extra_body_are_applied_and_thinking_is_stripped():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok("<think>hmm</think>\n  EMPTY "))

    out = _client(handler, max_tokens=100, extra_body={"chat_template_kwargs": {"enable_thinking": False}}).read([(PNG, "q")], max_new=512)
    assert out == ["EMPTY"]
    assert seen["body"]["max_tokens"] == 100 and seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("response", [
    httpx.Response(500, json={"error": "boom"}),
    httpx.Response(200, json={"choices": []}),
    httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
    httpx.Response(200, content=b"not json"),
])
def test_any_bad_answer_fails_the_whole_set(response):
    with pytest.raises(ReaderUnavailable):
        _client(lambda request: response).read([(PNG, "a"), (PNG, "b")])


def test_an_unreachable_server_is_reader_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ReaderUnavailable, match="could not be used"):
        _client(handler).read([(PNG, "a")])


def _write(tmp_path, monkeypatch, data, **settings):
    path = tmp_path / "engines.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(config, "READER_ENGINES_FILE", str(path))
    monkeypatch.setattr(config, "READER_ENGINE", settings.get("engine", ""))
    return path


REGISTRY = {"default": "first", "engines": [
    {"name": "first", "kind": "service", "url": "http://svc.test", "token_env": "TEST_ENGINE_TOKEN"},
    {"name": "second", "kind": "openai_compatible", "url": "http://oai.test/v1", "model": "m", "concurrency": 3},
]}


def test_the_file_default_is_active_and_kinds_build_their_own_client(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_ENGINE_TOKEN", "from-env")
    _write(tmp_path, monkeypatch, REGISTRY)
    client = engines.get_client()
    assert isinstance(client, ReaderClient) and client.token == "from-env" and engines.engine_name(client) == "first"
    other = engines.get_client("second")
    assert isinstance(other, OpenAICompatibleClient) and other.concurrency == 3 and engines.engine_name(other) == "second"
    assert engines.available()


def test_the_environment_choice_beats_the_file_default_and_first_entry_is_the_last_resort(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, REGISTRY, engine="second")
    assert engines.engine_name(engines.get_client()) == "second"
    _write(tmp_path, monkeypatch, {"engines": REGISTRY["engines"]})
    assert engines.engine_name(engines.get_client()) == "first"


@pytest.mark.parametrize("data", [
    {"default": "nope", "engines": REGISTRY["engines"]},  # names an engine that is not there
    {"engines": [REGISTRY["engines"][0], REGISTRY["engines"][0]]},  # repeats a name
    {"engines": [{"name": "x", "kind": "carrier_pigeon", "url": "u"}]},  # unknown kind
    {"engines": [{"name": "x", "kind": "openai_compatible", "url": "http://oai.test/v1"}]},  # no model
    {"engines": [{"name": "x", "kind": "service", "url": "u", "stray": 1}]},  # a misspelt setting is not ignored
    {"engines": []},
])
def test_a_registry_that_cannot_be_used_is_unavailable_not_a_crash(tmp_path, monkeypatch, data):
    _write(tmp_path, monkeypatch, data)
    with pytest.raises(ReaderUnavailable):
        engines.get_client()
    assert engines.available() is False


def test_a_missing_or_broken_file_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "READER_ENGINES_FILE", str(tmp_path / "absent.json"))
    assert engines.available() is False
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config, "READER_ENGINES_FILE", str(tmp_path / "bad.json"))
    assert engines.available() is False


def test_without_a_file_the_old_settings_make_one_engine_called_default(monkeypatch):
    monkeypatch.setattr(config, "READER_ENGINES_FILE", "")
    monkeypatch.setattr(config, "READER_ENGINE", "")
    monkeypatch.setattr(config, "VLM_READER_URL", "")
    assert engines.available() is False
    monkeypatch.setattr(config, "VLM_READER_URL", "http://reader.test")
    client = engines.get_client()
    assert isinstance(client, ReaderClient) and client.url == "http://reader.test" and engines.engine_name(client) == "default"
