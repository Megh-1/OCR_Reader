"""
The registry of reader engines: which model reads the crops of a page is a CONFIGURATION choice, not a code one.

An engine is one named entry in the JSON file `READER_ENGINES_FILE` points at (see config/reader_engines.example.json):

    {"default": "qwen_waybill",
     "engines": [
       {"name": "qwen_waybill", "kind": "service", "url": "http://127.0.0.1:8700", "token_env": "VLM_READER_TOKEN"},
       {"name": "unlimited_ocr", "kind": "openai_compatible", "url": "http://gpu-box:8000/v1", "model": "baidu/Unlimited-OCR"}]}

`READER_ENGINE` (env) picks one by name; otherwise the file's `default`, otherwise its first entry. Switching models is editing that
choice and restarting the API and worker - no code. With no file, the old settings (`VLM_READER_URL`, `VLM_READER_TOKEN`) still work and
make one engine called "default" of kind `service`, so an existing deployment is unchanged.

Both kinds answer `read(items, max_new) -> replies` (client.ReaderClient, openai_client.OpenAICompatibleClient), which is all the
pipeline knows. A registry or file that cannot be used is `ReaderUnavailable`, never a crash: the page is read the ordinary way.

What a registry cannot do: a different model still has its own best wording of the question. The prompts are the ones the shipped
adapter was trained on (waybill.py); an engine that needs others is a prompt change measured with scripts/evaluate_reader.py first.
Secrets stay out of the file: use `token_env`, the name of an environment variable.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app import config
from app.modules.vlm_reader.client import ReaderClient, ReaderUnavailable
from app.modules.vlm_reader.openai_client import OpenAICompatibleClient

log = logging.getLogger(__name__)

CropReader = Union[ReaderClient, OpenAICompatibleClient]


class EngineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    kind: Literal["service", "openai_compatible"]
    url: str = ""
    model: str = ""  # openai_compatible: the model name the server serves
    token_env: str = ""  # name of the environment variable holding the bearer token
    timeout_s: Optional[float] = Field(default=None, gt=0)
    connect_timeout_s: Optional[float] = Field(default=None, gt=0)
    crops_per_request: Optional[int] = Field(default=None, ge=1)  # service
    concurrency: int = Field(default=8, ge=1, le=64)  # openai_compatible: requests in flight at once
    max_tokens: Optional[int] = Field(default=None, ge=1)  # openai_compatible: cap on a reply
    extra_body: dict = Field(default_factory=dict)  # openai_compatible: server-specific request fields

    def build(self, transport=None) -> CropReader:
        token = os.environ.get(self.token_env, "") if self.token_env else ""
        if self.kind == "service":
            client = ReaderClient(url=self.url, token=token, timeout=self.timeout_s, per_request=self.crops_per_request, transport=transport)
            client.name = self.name
            return client
        return OpenAICompatibleClient(url=self.url, model=self.model, token=token, name=self.name, timeout=self.timeout_s,
                                      connect_timeout=self.connect_timeout_s, concurrency=self.concurrency,
                                      max_tokens=self.max_tokens, extra_body=self.extra_body, transport=transport)


class Registry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default: Optional[str] = None
    engines: list[EngineSpec] = Field(default_factory=list)


def _legacy_registry() -> Registry:
    """No registry file: the pre-registry settings make the one engine, so an existing deployment keeps working."""
    if not config.VLM_READER_URL:
        return Registry()
    spec = EngineSpec(name="default", kind="service", url=config.VLM_READER_URL)
    return Registry(default="default", engines=[spec])


def load_registry() -> Registry:
    path = config.READER_ENGINES_FILE
    if not path:
        return _legacy_registry()
    try:
        registry = Registry.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:  # unreadable, not JSON, or not the expected shape
        raise ReaderUnavailable(f"the reader engines file {path} cannot be used: {exc}") from exc
    names = [e.name for e in registry.engines]
    if len(set(names)) != len(names):
        raise ReaderUnavailable(f"the reader engines file {path} repeats an engine name")
    return registry


def active_name(registry: Registry, name: Optional[str] = None) -> Optional[str]:
    chosen = name or config.READER_ENGINE or registry.default or (registry.engines[0].name if registry.engines else None)
    return chosen


def get_client(name: Optional[str] = None, transport=None) -> CropReader:
    """The engine named `name`, else the active one. Raises ReaderUnavailable when there is none or it is not set up."""
    registry = load_registry()
    chosen = active_name(registry, name)
    if chosen is None:
        raise ReaderUnavailable("no reader engine is configured (set READER_ENGINES_FILE, or VLM_READER_URL)")
    for spec in registry.engines:
        if spec.name == chosen:
            client = spec.build(transport=transport)
            if not client.configured:
                raise ReaderUnavailable(f"reader engine {chosen!r} has no url" + ("" if spec.kind == "service" else " or model"))
            return client
    raise ReaderUnavailable(f"reader engine {chosen!r} is not in the registry ({[e.name for e in registry.engines]})")


def available() -> bool:
    """Whether an engine is set up at all: what turns the vision reader on for a template that asks for it."""
    try:
        get_client()
    except ReaderUnavailable as exc:
        if config.READER_ENGINES_FILE:
            log.warning("vision reader off: %s", exc)
        return False
    return True


def engine_name(client: CropReader) -> str:
    return getattr(client, "name", "") or "default"
