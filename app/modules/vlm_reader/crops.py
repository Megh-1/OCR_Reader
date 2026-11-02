"""
Cuts, from one aligned page, the window the vision model reads for every field.

One crop per field, except the cells that hold several values (a party cell: name + address; the goods cell: description + HS code +
invoice), cut once and asked for together. Each is cut at every widening level (waybill.LEVELS). A window is the field's printed
cell (nearest rules, from the profile) plus a little slop, moved by page alignment, then by two measured corrections:

  registration  a form fed slightly off in a printer has its TYPED layer shifted against the printed form, the same way across a zone
                of the page. The zone's value boxes are slid over the typed ink (the page's ink minus the form's own printed text) and
                the shift kept, only when it holds at least REGISTER_MIN_GAIN times the typed ink of no shift. Ungated, it made real
                pages worse (share of values fully inside the tight window 72 -> 55% on Bangkok, 81 -> 65% on Mumbai).
  snap          each edge moves to take in, or let go of, a block of text it would cut (a value clipped by the window was a measured
                failure: 1432.0 for 1432.00, 176-060 for 176-06691322).

Moved here from scripts/make_crops.py, which now imports it, so the windows that were measured are the windows that ship.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from app.modules.alignment import Alignment
from app.modules.vlm_reader import waybill as W
from app.modules.vlm_reader.profile import Profile
from app.modules.vlm_reader.words import lines_of
from app.schemas.template import TemplateField

SNAP_MAX_X, SNAP_MAX_Y = 0.03, 0.012  # how far an edge may move to take in, or let go of, a block of text (page fractions)
SNAP_KEEP = 0.4  # a block with at least this share inside the window belongs to it; less, and it is a neighbour peeking in
SNAP_BARRIER = False  # keep printed rules as walls between text blocks

REGISTER_MAX_X, REGISTER_MAX_Y, REGISTER_STEP = 0.03, 0.045, 0.002  # the typed layer is never further off than this (scripts/drift.py)
REGISTER_TIE = 0.97  # shifts scoring within this share of the best are ties, and the smallest one wins (a short value fits many shifts)
REGISTER_MIN_GAIN = 2.0


def text_blobs(page: Image.Image) -> list[tuple[float, float, float, float]]:
    """Runs of text on a page as page-fraction boxes, from the ink alone: printed rules taken out, then letters joined across a word
    space. No OCR, so a value the page OCR could not read still has a block."""
    gray = np.array(page.convert("L"))
    h, w = gray.shape
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, max(3, int(w * 0.025))), np.uint8)) | cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((max(3, int(h * 0.02)), 1), np.uint8))
    lines = cv2.dilate(lines, np.ones((3, 3), np.uint8))
    text = cv2.bitwise_and(binary, cv2.bitwise_not(lines))
    text = cv2.morphologyEx(text, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))  # scan speckle
    gap = max(3, int(w * 0.009))
    joined = cv2.dilate(text, np.ones((1, gap), np.uint8))
    if SNAP_BARRIER:
        joined = cv2.bitwise_and(joined, cv2.bitwise_not(lines))
    n, _, stats, _ = cv2.connectedComponentsWithStats(joined, connectivity=8)
    out = []
    for x, y, bw, bh, _area in stats[1:n]:
        if 0.003 * h <= bh <= 0.03 * h and bw >= 0.004 * w:
            out.append(((x + gap / 2) / w, y / h, (x + bw - gap / 2) / w, (y + bh) / h))
    return out


def snap(box, blobs):
    """Moves each edge of a page-fraction window so that it does not cut a block of text: a block mostly inside is taken in whole (a
    value the window would have clipped), a block mostly outside is let go (a neighbouring cell's text). Bounded, and a window never
    shrinks below half its size."""
    x0, y0, x1, y1 = box
    grow, shrink = list(box), list(box)
    for b in blobs:
        if b[2] <= x0 or b[0] >= x1 or b[3] <= y0 or b[1] >= y1:
            continue
        bw, bh = b[2] - b[0], b[3] - b[1]
        if bw <= 0 or bh <= 0:
            continue
        over_y = (min(b[3], y1) - max(b[1], y0)) / bh
        over_x = (min(b[2], x1) - max(b[0], x0)) / bw
        if over_y >= 0.5:  # beside the window
            if b[0] < x0 < b[2]:
                if over_x >= SNAP_KEEP and x0 - b[0] <= SNAP_MAX_X:
                    grow[0] = min(grow[0], b[0])
                elif over_x < SNAP_KEEP and b[2] - x0 <= SNAP_MAX_X:
                    shrink[0] = max(shrink[0], b[2])
            if b[0] < x1 < b[2]:
                if over_x >= SNAP_KEEP and b[2] - x1 <= SNAP_MAX_X:
                    grow[2] = max(grow[2], b[2])
                elif over_x < SNAP_KEEP and x1 - b[0] <= SNAP_MAX_X:
                    shrink[2] = min(shrink[2], b[0])
        if over_x >= 0.5:  # above or below it
            if b[1] < y0 < b[3]:
                if over_y >= SNAP_KEEP and y0 - b[1] <= SNAP_MAX_Y:
                    grow[1] = min(grow[1], b[1])
                elif over_y < SNAP_KEEP and b[3] - y0 <= SNAP_MAX_Y:
                    shrink[1] = max(shrink[1], b[3])
            if b[1] < y1 < b[3]:
                if over_y >= SNAP_KEEP and b[3] - y1 <= SNAP_MAX_Y:
                    grow[3] = max(grow[3], b[3])
                elif over_y < SNAP_KEEP and y1 - b[1] <= SNAP_MAX_Y:
                    shrink[3] = min(shrink[3], b[1])
    out = [grow[0] if grow[0] < x0 else shrink[0], grow[1] if grow[1] < y0 else shrink[1],
           grow[2] if grow[2] > x1 else shrink[2], grow[3] if grow[3] > y1 else shrink[3]]
    if out[2] - out[0] < 0.5 * (x1 - x0):
        out[0], out[2] = min(out[0], x0) if out[0] < x0 else x0, max(out[2], x1) if out[2] > x1 else x1
    if out[3] - out[1] < 0.5 * (y1 - y0):
        out[1], out[3] = min(out[1], y0) if out[1] < y0 else y0, max(out[3], y1) if out[3] > y1 else y1
    return tuple(out)


def _text_mask(page: Image.Image) -> np.ndarray:
    gray = np.array(page.convert("L"))
    h, w = gray.shape
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, max(3, int(w * 0.025))), np.uint8)) | cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((max(3, int(h * 0.02)), 1), np.uint8))
    return cv2.bitwise_and(binary, cv2.bitwise_not(cv2.dilate(lines, np.ones((3, 3), np.uint8))))


def register_zones(page: Image.Image, al: Alignment, profile: Profile, fields: dict[str, TemplateField]) -> dict[str, tuple[float, float]]:
    """How far the TYPED layer sits from the printed form, zone by zone (page fractions). Page alignment places the form; the
    typed text of a form fed slightly off is shifted against it. Found by sliding the template's value boxes over the typed ink and
    keeping the shift that holds the most, the smallest such shift on a tie, and only when the evidence is strong."""
    text = _text_mask(page)
    h, w = text.shape
    for b in profile.form_blobs:
        m = al.map_box(b)
        text[max(0, int(m[1] * h) - 1):int(m[3] * h) + 2, max(0, int(m[0] * w) - 1):int(m[2] * w) + 2] = 0
    integral = cv2.integral((text > 0).astype(np.uint8))
    grid_x = [i * REGISTER_STEP for i in range(-int(REGISTER_MAX_X / REGISTER_STEP), int(REGISTER_MAX_X / REGISTER_STEP) + 1)]
    grid_y = [i * REGISTER_STEP for i in range(-int(REGISTER_MAX_Y / REGISTER_STEP), int(REGISTER_MAX_Y / REGISTER_STEP) + 1)]
    zones: dict[str, set] = {}
    for fid, f in fields.items():
        if f.from_field is None:
            zones.setdefault(W.ZONE[fid], set()).add(tuple(al.map_box(f.bbox)))
    out = {}
    for zone, boxes in zones.items():
        scored = []
        for dy in grid_y:
            for dx in grid_x:
                total = 0
                for b in boxes:
                    x0, x1 = max(0, min(w, int((b[0] + dx) * w))), max(0, min(w, int((b[2] + dx) * w)))
                    y0, y1 = max(0, min(h, int((b[1] + dy) * h))), max(0, min(h, int((b[3] + dy) * h)))
                    total += int(integral[y1, x1]) - int(integral[y0, x1]) - int(integral[y1, x0]) + int(integral[y0, x0])
                scored.append((total, dx, dy))
        best = max(s[0] for s in scored)
        ties = [s for s in scored if s[0] >= REGISTER_TIE * best]
        chosen, dx, dy = min(ties, key=lambda s: abs(s[1]) + abs(s[2]))
        at_zero = next(s[0] for s in scored if abs(s[1]) < 1e-9 and abs(s[2]) < 1e-9)
        out[zone] = (dx, dy) if at_zero == 0 or chosen >= REGISTER_MIN_GAIN * at_zero else (0.0, 0.0)
    return out


def cell_rect(profile: Profile, box):
    """The printed cell around a template box: the nearest rule on each side, found from the form's rule geometry."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    ups = [y for a, b, y in profile.rules_h if a - 0.002 <= cx <= b + 0.002 and y <= box[1] + 0.002]
    dns = [y for a, b, y in profile.rules_h if a - 0.002 <= cx <= b + 0.002 and y >= box[3] - 0.002]
    lfs = [x for x, a, b in profile.rules_v if a - 0.002 <= cy <= b + 0.002 and x <= box[0] + 0.002]
    rts = [x for x, a, b in profile.rules_v if a - 0.002 <= cy <= b + 0.002 and x >= box[2] - 0.002]
    return (max(lfs) if lfs else box[0], max(ups) if ups else box[1], min(rts) if rts else box[2], min(dns) if dns else box[3])


def search_box(profile: Profile, fields: dict[str, TemplateField], fid: str, widen: float = 0.0):
    """Template-frame search region for a field: its printed cell plus a little slop, plus `widen` on every side. A data row of the
    cargo table is only searched a few lines down from the header, not the whole column."""
    f = fields[fid]
    cell = cell_rect(profile, f.bbox)
    g = W.SLOP + widen
    box = (cell[0] - g, cell[1] - g, cell[2] + g, cell[3] + g)
    if fid in W.TABLE_ROW:
        box = (box[0], box[1], box[2], min(box[3], f.bbox[3] + 0.020 + widen))
    return box


def captions_in(profile: Profile, box) -> str:
    """The form's own printed words inside a template-frame region, in reading order: what the model must never return"""
    words = [t for t in profile.printed if not any(c.isdigit() for c in t[0]) and box[0] <= (t[1] + t[3]) / 2 <= box[2] and box[1] <= (t[2] + t[4]) / 2 <= box[3]]
    return " ".join(t[0] for ln in lines_of(words) for t in ln)


@dataclass
class Crop:
    kind: str  # "single" | "party" | "goods"
    fields: list[str]
    label: str
    captions: str
    hint: str
    level: int
    region: list[float]  # where the window sits on the page, as page fractions
    image: Image.Image

    def png(self) -> bytes:
        buf = io.BytesIO()
        self.image.save(buf, format="PNG")
        return buf.getvalue()


def crops_for_page(page: Image.Image, al: Alignment, profile: Profile, fields: dict[str, TemplateField], levels=W.LEVELS) -> list[Crop]:
    """Every field's window, at each widening level, cut from an already-aligned page."""
    blobs = text_blobs(page)
    shifts = register_zones(page, al, profile, fields)
    out: list[Crop] = []

    def cut_entry(box, zone):
        m = al.map_box(box)
        dx, dy = shifts.get(zone, (0.0, 0.0))
        m = snap((max(0.0, m[0] + dx), max(0.0, m[1] + dy), min(1.0, m[2] + dx), min(1.0, m[3] + dy)), blobs)
        x0, y0, x1, y1 = (max(0.0, m[0] - W.CONTEXT), max(0.0, m[1] - W.CONTEXT), min(1.0, m[2] + W.CONTEXT), min(1.0, m[3] + W.CONTEXT))
        crop = page.crop((int(x0 * page.width), int(y0 * page.height), int(x1 * page.width), int(y1 * page.height)))
        return [round(c, 4) for c in m], crop

    for lv, widen in enumerate(levels):
        for fid in fields:
            if fid in W.SKIP:
                continue
            sb = search_box(profile, fields, fid, widen)
            region, img = cut_entry(sb, W.ZONE[fid])
            out.append(Crop("single", [fid], fields[fid].label, captions_in(profile, sb), W.HINTS.get(fid, ""), lv, region, img))
        for cell, (n, a_) in W.PARTY.items():
            b1, b2 = search_box(profile, fields, n, widen), search_box(profile, fields, a_, widen)
            sb = (min(b1[0], b2[0]), min(b1[1], b2[1]), max(b1[2], b2[2]), max(b1[3], b2[3]))
            region, img = cut_entry(sb, W.ZONE[n])
            out.append(Crop("party", [n, a_], cell, captions_in(profile, sb), "", lv, region, img))
        sb = search_box(profile, fields, "nature_of_goods", widen)
        region, img = cut_entry(sb, W.ZONE["nature_of_goods"])
        out.append(Crop("goods", list(W.GOODS), "goods", captions_in(profile, sb), "", lv, region, img))
    return out
