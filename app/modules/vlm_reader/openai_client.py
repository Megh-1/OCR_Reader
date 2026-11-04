"""
A crop reader for any server that speaks the OpenAI chat-completions API with image input: vLLM, SGLang, Ollama, LM Studio, or a hosted
vision model. Which MODEL answers is the server's business (the `model` name in the registry entry), so a newly published model needs a
new registry entry and, at most, its own prompt - not new application code.

It answers the same question as `ReaderClient` (crops and questions go out, one reply per crop comes back, all-or-nothing), so the
rest of the pipeline cannot tell the two apart. Crops are sent one per request, several at once: that is how these servers batch.
"""

from __future__ import annotations

import base64
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Sequence

import httpx

from app import config
from app.modules.vlm_reader.client import ReaderUnavailable

log = logging.getLogger(__name__)

_THINK = re.compile(r"<think>.*?</think>", re.S)


class OpenAICompatibleClient:
    kind = "openai_compatible"

    def __init__(self, url: str, model: str, token: str = "", name: str = "", timeout: Optional[float] = None,
                 connect_timeout: Optional[float] = None, concurrency: int = 8, max_tokens: Optional[int] = None,
                 extra_body: Optional[dict] = None, transport: Optional[httpx.BaseTransport] = None):
        self.url = url.rstrip("/")  # the API base, e.g. http://host:8000/v1
        self.model = model
        self.token = token
        self.name = name or model
        self.timeout = timeout if timeout is not None else config.VLM_READER_TIMEOUT_S
        self.connect_timeout = connect_timeout if connect_timeout is not None else config.VLM_READER_CONNECT_TIMEOUT_S
        self.concurrency = max(1, concurrency)
        self.max_tokens = max_tokens
        self.extra_body = extra_body or {}
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url and self.model)

    def _body(self, png: bytes, prompt: str, max_new: int) -> dict:
        data_uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": prompt},
            ]}],
            "max_tokens": min(max_new, self.max_tokens) if self.max_tokens else max_new,
            "temperature": 0,
        }
        body.update(self.extra_body)  # server-specific switches (e.g. a model that thinks by default) win over the defaults
        return body

    @staticmethod
    def _reply(payload: object) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as exc:
            raise ReaderUnavailable("the reader returned a malformed reply") from exc
        if not isinstance(content, str):
            raise ReaderUnavailable("the reader returned no text for a crop")
        return _THINK.sub("", content).strip()

    def read(self, items: Sequence[tuple[bytes, str]], max_new: int = 512) -> list[str]:
        """`items`: (PNG bytes, question) per crop. Returns one reply per item, in order; any failure fails the whole set."""
        if not self.configured:
            raise ReaderUnavailable(f"reader engine {self.name!r} has no url or model")
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        timeout = httpx.Timeout(self.timeout, connect=min(self.timeout, self.connect_timeout))
        try:
            with httpx.Client(timeout=timeout, transport=self._transport, headers=headers) as http:
                def one(item: tuple[bytes, str]) -> str:
                    response = http.post(f"{self.url}/chat/completions", json=self._body(item[0], item[1], max_new))
                    response.raise_for_status()
                    return self._reply(response.json())

                with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
                    return list(pool.map(one, items))  # map keeps the order of `items`
        except ReaderUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:  # connection, timeout, bad status, unreadable JSON
            raise ReaderUnavailable(f"reader engine {self.name!r} could not be used: {exc}") from exc
