"""
A reader profile: what one printed FORM looks like, beyond what a template stores, so a crop can be cut to a printed cell and the
form's own words can be told from a typed value.

Built from the one reference page a template was cut from, by scripts/build_reader_profile.py:

  rules         the form's printed horizontal and vertical rules, as page fractions in the template frame: h = (x0, x1, y), v = (x, y0, y1)
  printed       the reference page's words that sit OUTSIDE every value box: the form's own labels, as (text, x0, y0, x1, y1, conf)
  form_blobs    the form's own printed text as template-frame boxes (the reference page with its typed values taken out)

A profile is made from a real document, so it can carry whatever was printed outside the value boxes of that page. It is therefore NOT
committed (data/reader_profiles/ is git-ignored) and is found through config.VLM_READER_PROFILE_DIR.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import VLM_READER_PROFILE_DIR

Tok = tuple  # (text, x0, y0, x1, y1, conf)
_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class ProfileMissing(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    rules_h: tuple[tuple[float, float, float], ...]
    rules_v: tuple[tuple[float, float, float], ...]
    printed: tuple[Tok, ...]
    form_blobs: tuple[tuple[float, float, float, float], ...]


def from_dict(name: str, d: dict) -> Profile:
    return Profile(
        name=name,
        rules_h=tuple(tuple(r) for r in d["rules"]["h"]),
        rules_v=tuple(tuple(r) for r in d["rules"]["v"]),
        printed=tuple(tuple(t) for t in d["printed"]),
        form_blobs=tuple(tuple(b) for b in d["form_blobs"]),
    )


@lru_cache(maxsize=8)
def load_profile(name: str) -> Profile:
    if not _NAME.match(name):
        raise ProfileMissing(f"{name!r} is not a profile name")
    path = Path(VLM_READER_PROFILE_DIR) / f"{name}.json"
    if not path.is_file():
        raise ProfileMissing(f"reader profile {name!r} not found at {path}")
    return from_dict(name, json.loads(path.read_text(encoding="utf-8")))
