"""
The client of the reader service (scripts/gpu/serve_reader.py): crops and questions go out, the model's replies come back.

The service holds the 7B model and its adapter on a GPU machine; this app does not. Any failure to get a complete set of replies
(not configured, unreachable, a bad status, a short answer) is `ReaderUnavailable`, which the pipeline answers by reading the page
the ordinary way. A reply set is all-or-nothing: a page is never half read by the model and half guessed.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional, Sequence

import httpx

from app import config

log = logging.getLogger(__name__)


class ReaderUnavailable(Exception):
    """The vision reader cannot be used for this page; read it the ordinary way."""


class ReaderClient:
    kind = "service"
    name = "default"  # set by the engine registry (engines.py) to the engine's name

    def __init__(self, url: Optional[str] = None, token: Optional[str] = None, timeout: Optional[float] = None,
                 per_request: Optional[int] = None, transport: Optional[httpx.BaseTransport] = None):
        self.url = (url if url is not None else config.VLM_READER_URL).rstrip("/")
        self.token = token if token is not None else config.VLM_READER_TOKEN
        self.timeout = timeout if timeout is not None else config.VLM_READER_TIMEOUT_S
        self.per_request = per_request if per_request is not None else config.VLM_READER_CROPS_PER_REQUEST
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def read(self, items: Sequence[tuple[bytes, str]], max_new: int = 512) -> list[str]:
        """`items`: (PNG bytes, question) per crop. Returns one reply per item, in order."""
        if not self.configured:
            raise ReaderUnavailable("VLM_READER_URL is not set")
        replies: list[str] = []
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, config.VLM_READER_CONNECT_TIMEOUT_S)), transport=self._transport, headers=headers) as http:
                for start in range(0, len(items), self.per_request):
                    chunk = items[start:start + self.per_request]
                    body = {"max_new": max_new, "requests": [{"image_b64": base64.b64encode(png).decode("ascii"), "prompt": prompt} for png, prompt in chunk]}
                    response = http.post(f"{self.url}/read", json=body)
                    response.raise_for_status()
                    got = response.json().get("replies")
                    if not isinstance(got, list) or len(got) != len(chunk) or not all(isinstance(r, str) for r in got):
                        raise ReaderUnavailable("the reader service returned a malformed or short reply")
                    replies.extend(got)
        except ReaderUnavailable:
            raise
        except (httpx.HTTPError, ValueError) as exc:  # connection, timeout, bad status, unreadable JSON
            raise ReaderUnavailable(f"the reader service could not be used: {exc}") from exc
        return replies
