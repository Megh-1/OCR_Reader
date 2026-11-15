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
    kind: Literal["tesseract", "text_layer", "openai_compatible", "service", "existing_service"]
    url: str = ""
    model: str = ""  # openai_compatible: the model name the server serves
    token_env: str = ""  # name of the environment variable holding the bearer token
    timeout_s: Optional[float] = Field(default=None, gt=0)
    connect_timeout_s: Optional[float] = Field(default=None, gt=0)
    crops_per_request: Optional[int] = Field(default=None, ge=1)  # service
    concurrency: int = Field(default=8, ge=1, le=64)  # openai_compatible: requests in flight at once
    max_tokens: Optional[int] = Field(default=None, ge=1)  # openai_compatible: cap on a reply
    extra_body: dict = Field(default_factory=dict)  # openai_compatible: server-specific request fields
    role: Literal["crop", "page"] = "crop"  # crop: reads template-guided crops (this module); page: reads a whole page with no template (app/modules/page_reader.py)
    prompt_file: str = ""  # JSON {"party": ..., "goods": ..., "single": ...}: this engine's own wording of the questions (waybill.py has the shipped ones)

    @property
    def is_reader(self) -> bool:
        """A crop reader, i.e. a model behind a URL. `tesseract` and `text_layer` are the pipeline's own paths, not something to call."""
        return self.role == "crop" and self.kind in ("service", "existing_service", "openai_compatible")

    @property
    def prior(self) -> int:
        """A fixed ordering for a template nobody has measured yet (lower = earlier): the engine fine-tuned on our forms first, a
        general model after it. A starting guess, replaced by the measurements as soon as there are any."""
        return {"service": 0, "existing_service": 0, "openai_compatible": 1}.get(self.kind, 9)

    def prompts(self) -> Optional[dict]:
        """This engine's own question wording, or None for the shipped one. A file that cannot be read is an unusable engine."""
        if not self.prompt_file:
            return None
        try:
            data = json.loads(Path(self.prompt_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReaderUnavailable(f"engine {self.name!r}: prompt file {self.prompt_file} cannot be used: {exc}") from exc
        if not isinstance(data, dict) or not data or not set(data) <= {"party", "goods", "single"} or not all(isinstance(v, str) for v in data.values()):
            raise ReaderUnavailable(f"engine {self.name!r}: prompt file {self.prompt_file} must map party/goods/single to text")
        return data

    def build(self, transport=None) -> CropReader:
        if not self.is_reader:
            raise ReaderUnavailable(f"engine {self.name!r} is the pipeline's own {self.kind} path, not a crop reader")
        token = os.environ.get(self.token_env, "") if self.token_env else ""
        if self.kind in ("service", "existing_service"):
            client = ReaderClient(url=self.url, token=token, timeout=self.timeout_s, per_request=self.crops_per_request, transport=transport)
            client.name = self.name
            client.prompts = self.prompts()
            return client
        client = OpenAICompatibleClient(url=self.url, model=self.model, token=token, name=self.name, timeout=self.timeout_s,
                                      connect_timeout=self.connect_timeout_s, concurrency=self.concurrency,
                                      max_tokens=self.max_tokens, extra_body=self.extra_body, transport=transport)
        client.prompts = self.prompts()
        return client


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


def page_specs(registry: Optional[Registry] = None) -> list[EngineSpec]:
    """The engines that read a whole page with no template (role "page"), in the registry's order."""
    return [e for e in (registry or load_registry()).engines if e.role == "page" and e.kind == "openai_compatible"]


def reader_specs(registry: Optional[Registry] = None) -> list[EngineSpec]:
    """The engines that are crop readers, in the registry's order (which the selector uses only as the last tiebreak)."""
    return [e for e in (registry or load_registry()).engines if e.is_reader]


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
                raise ReaderUnavailable(f"reader engine {chosen!r} has no url" + ("" if spec.kind != "openai_compatible" else " or model"))
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
