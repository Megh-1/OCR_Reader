"""
Text-layer registration: where THIS page's typed words sit against the template's boxes.

A template's boxes are fractions of the page, and the alignment that moves them is fitted on the PRINTED labels (raster). The typed text
of a pre-printed form is a separate thing: a form fed slightly off in a printer is typed on its own offset, zone by zone, and the alignment
cannot see it. Measured on the dev corpus (docs/tesseract-baseline.md) that was 419 of the 471 missing digital fields (cause LOC): the boxes
read a neighbour's words.

What decides a shift uses no value and no label, only what the template already says:
  * occupancy: a box of one line that holds a typed word at that shift (the typed words of a page are the layer; the boxes mark where
    typed words go), and how well centred the word is on it;
  * format: a field that has a `validation_regex` and whose read at that shift is non-empty and passes it (Module 7's own test).
For each field the two are summed over the fields within `TEXT_LAYER_REG_NEIGHBOURHOOD` of it (a zone of the page moves together) and the
shift with the highest sum wins, used only when it beats the unshifted position by `TEXT_LAYER_REG_MIN_GAIN`; a tie goes to the smaller
shift. Nothing is learned. Fields read through a shift are reported so the caller can cap them at spot_check; this module never raises a tier.
"""

from __future__ import annotations

import re

import numpy as np

from app.config import (
    TEXT_LAYER_MIN_WORD_OVERLAP,
    TEXT_LAYER_REG_FORMAT_WEIGHT,
    TEXT_LAYER_REG_MAX_BOX_HEIGHT,
    TEXT_LAYER_REG_MAX_DX,
    TEXT_LAYER_REG_MAX_DY,
    TEXT_LAYER_REG_MIN_GAIN,
    TEXT_LAYER_REG_NEIGHBOURHOOD,
    TEXT_LAYER_REG_STEP,
    TEXT_LAYER_REG_STEP_X,
)
from app.schemas.template import TemplateField
from app.models.text_layer import TextLayer


def _grid() -> np.ndarray:
    xs = np.arange(-TEXT_LAYER_REG_MAX_DX, TEXT_LAYER_REG_MAX_DX + 1e-9, TEXT_LAYER_REG_STEP_X)
    ys = np.arange(-TEXT_LAYER_REG_MAX_DY, TEXT_LAYER_REG_MAX_DY + 1e-9, TEXT_LAYER_REG_STEP)
    return np.array([(x, y) for y in ys for x in xs])  # (S, 2)


def _reading_order(idx: np.ndarray, words: np.ndarray, texts: list[str]) -> str:
    cy = (words[idx, 1] + words[idx, 3]) / 2
    order = idx[np.argsort(cy, kind="stable")]
    # words of one line left to right (a line: centres within half a word height of the first of the run)
    lines: list[list[int]] = []
    for i in order:
        h = words[i, 3] - words[i, 1]
        c = (words[i, 1] + words[i, 3]) / 2
        if lines and abs(c - np.mean([(words[j, 1] + words[j, 3]) / 2 for j in lines[-1]])) <= 0.5 * max(h, max(words[j, 3] - words[j, 1] for j in lines[-1])):
            lines[-1].append(i)
        else:
            lines.append([i])
    return " ".join(texts[j] for line in lines for j in sorted(line, key=lambda j: words[j, 0]))


def register_boxes(layer: TextLayer, fields: list[TemplateField]) -> dict[str, tuple[float, float]]:
    """{field id: (dx, dy)} for the fields whose box should move; a field not in the result stays where it is. `fields` carry the
    boxes as they sit on this page (aligned)."""
    direct = [f for f in fields if f.from_field is None]
    if not layer.words or len(direct) < 3:
        return {}
    words = np.array([w.bbox for w in layer.words], dtype=float)  # (N, 4)
    texts = [w.text for w in layer.words]
    area = np.maximum((words[:, 2] - words[:, 0]) * (words[:, 3] - words[:, 1]), 1e-12)
    box = np.array([f.bbox for f in direct], dtype=float)  # (B, 4)
    shifts = _grid()  # (S, 2)
    S = len(shifts)
    zero = int(np.argmin(np.abs(shifts).sum(axis=1)))
    single = (box[:, 3] - box[:, 1]) <= TEXT_LAYER_REG_MAX_BOX_HEIGHT
    patterns = [re.compile(f.validation_regex) if f.validation_regex and f.extract_pattern is None and not f.squash_spaces else None for f in direct]
    wcy = (words[:, 1] + words[:, 3]) / 2

    # per field and shift: how well the box is occupied (one line boxes) and whether the read passes the field's format
    occupancy = np.zeros((len(direct), S))
    valid = np.zeros((len(direct), S))
    for k, b in enumerate(box):
        near = np.flatnonzero(np.abs(wcy - (b[1] + b[3]) / 2) <= (b[3] - b[1]) / 2 + TEXT_LAYER_REG_MAX_DY + 0.02)
        if len(near) == 0:
            continue
        w = words[near]
        bx0 = b[0] + shifts[:, 0][:, None]
        bx1 = b[2] + shifts[:, 0][:, None]
        by0 = b[1] + shifts[:, 1][:, None]
        by1 = b[3] + shifts[:, 1][:, None]
        iw = np.clip(np.minimum(bx1, w[:, 2][None, :]) - np.maximum(bx0, w[:, 0][None, :]), 0, None)
        ih = np.clip(np.minimum(by1, w[:, 3][None, :]) - np.maximum(by0, w[:, 1][None, :]), 0, None)
        read = (iw * ih / area[near][None, :]) >= TEXT_LAYER_MIN_WORD_OVERLAP  # (S, n)
        if single[k]:
            centred = np.clip(1.0 - np.abs(wcy[near][None, :] - (by0 + by1) / 2) / max(b[3] - b[1], 1e-6), 0.0, 1.0)
            occupancy[k] = np.where(read, 1.0 + centred, 0.0).max(axis=1)
        if patterns[k] is not None:
            cache: dict[tuple, bool] = {}
            for s in range(S):
                hit = tuple(near[read[s]])
                if not hit:
                    continue
                if hit not in cache:
                    cache[hit] = patterns[k].fullmatch(_reading_order(np.array(hit), words, texts).strip()) is not None
                valid[k, s] = cache[hit]

    cy = (box[:, 1] + box[:, 3]) / 2
    out: dict[str, tuple[float, float]] = {}
    for k, f in enumerate(direct):
        mine = np.abs(cy - cy[k]) <= TEXT_LAYER_REG_NEIGHBOURHOOD
        score = occupancy[mine].sum(axis=0) + TEXT_LAYER_REG_FORMAT_WEIGHT * valid[mine].sum(axis=0)
        best = score.max()
        if best < score[zero] + TEXT_LAYER_REG_MIN_GAIN:
            continue
        tied = np.flatnonzero(score >= best - 1e-9)
        pick = tied[np.argmin(np.abs(shifts[tied]).sum(axis=1))]
        if pick != zero:
            out[f.id] = (float(shifts[pick, 0]), float(shifts[pick, 1]))
    return out


def shifted(box: tuple[float, float, float, float], shift: tuple[float, float]) -> tuple[float, float, float, float]:
    return (box[0] + shift[0], box[1] + shift[1], box[2] + shift[0], box[3] + shift[1])
