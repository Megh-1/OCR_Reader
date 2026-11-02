"""
The second, independent reader: it never recognises anything itself. It takes the words ONE page-level OCR already found, drops the
ones that are the form's own printed labels, and picks the words that fit each field (a number for a weight, three capitals for an
airport code, the first typed line of a party cell for a name). Nothing is learned.

It exists to be compared with the vision model: a value both read identically is much stronger evidence than either alone (decide.py).
Moved here from scripts/proto_select.py, which now imports it, so the reader that was measured is the reader that ships.
"""

from __future__ import annotations

import re
from collections import defaultdict

import pytesseract
from PIL import Image
from rapidfuzz import fuzz

from app.modules.alignment import Alignment
from app.modules.vlm_reader import waybill as W
from app.modules.vlm_reader.profile import Profile
from app.schemas.template import TemplateField

Tok = tuple  # (text, x0, y0, x1, y1, conf)


def norm(s: str) -> str:
    return " ".join((s or "").split())


def key(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s).lower()


def centre(t: Tok) -> tuple[float, float]:
    return (t[1] + t[3]) / 2, (t[2] + t[4]) / 2


def lines_of(tokens: list[Tok]) -> list[list[Tok]]:
    """Reading-order lines: tokens whose vertical centres sit within about half a line of each other."""
    out: list[list[Tok]] = []
    for t in sorted(tokens, key=lambda t: centre(t)[1]):
        h = max(t[4] - t[2], 0.006)
        if out and abs(centre(t)[1] - sum(centre(u)[1] for u in out[-1]) / len(out[-1])) <= 0.55 * max(h, max(u[4] - u[2] for u in out[-1])):
            out[-1].append(t)
        else:
            out.append([t])
    return [sorted(l, key=lambda t: t[1]) for l in out]


def join(tokens: list[Tok]) -> str:
    return " ".join(t[0] for l in lines_of(tokens) for t in l)


def page_tokens(image: Image.Image, psm: int = 3) -> list[Tok]:
    """Every word on a page with its box (page fractions) and Tesseract's confidence, from ONE whole-page read."""
    d = pytesseract.image_to_data(image, config=f"--psm {psm}", output_type=pytesseract.Output.DICT)
    out = []
    for i, t in enumerate(d["text"]):
        if not t.strip() or float(d["conf"][i]) < 0:
            continue
        x0, y0 = d["left"][i] / image.width, d["top"][i] / image.height
        out.append((t, x0, y0, x0 + d["width"][i] / image.width, y0 + d["height"][i] / image.height, float(d["conf"][i])))
    return out


def clean(tokens: list[Tok]) -> list[Tok]:
    """Drop what is not a word: rule fragments and OCR junk (no letter or digit), and very low-confidence specks."""
    out = []
    for t in tokens:
        alnum = sum(c.isalnum() for c in t[0])
        if alnum == 0 or (t[5] < 25 and alnum <= 2):
            continue
        text = re.sub(r"^[^A-Za-z0-9(\"'+]+", "", t[0])  # a leading rule glyph ("|DXB") is not part of the word
        out.append((text, *t[1:]))
    return out


def printed_words(profile: Profile) -> list[tuple[str, float, float]]:
    """The form's own words as (key, centre x, centre y) in the template frame."""
    return [(key(t[0]), *centre(t)) for t in profile.printed if key(t[0])]


def label_mask(tokens: list[Tok], al: Alignment, printed) -> set[int]:
    """indices of tokens that are a printed label here: same word as a printed one, at where alignment says it should be"""
    where = []
    for w, cx, cy in printed:
        b = al.map_box((cx - 0.003, cy - 0.003, cx + 0.003, cy + 0.003))
        where.append((w, (b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
    by_word = defaultdict(list)
    for w, x, y in where:
        by_word[w].append((x, y))
    mask = set()
    for i, t in enumerate(tokens):
        k = key(t[0])
        if not k:
            continue
        cx, cy = centre(t)
        for w in ([k] if k in by_word else [w for w in by_word if len(w) > 3 and abs(len(w) - len(k)) <= 1 and fuzz.ratio(w, k) >= 88]):
            if any(abs(cy - y) <= 0.012 and abs(cx - x) <= 0.05 for x, y in by_word[w]):
                mask.add(i)
                break
    return mask


def region(al: Alignment, fields: dict[str, TemplateField], field_id: str):
    b = al.map_box(fields[field_id].bbox)
    ex, ey = W.EXPAND.get(field_id, W.DEFAULT_EXPAND)
    return (b[0] - ex, b[1] - ey, b[2] + ex, b[3] + ey)


def pool(tokens, mask, claimed, reg):
    """(index, token) of the words inside a region that are neither a printed label nor already taken by another field"""
    return [(i, t) for i, t in enumerate(tokens)
            if i not in mask and i not in claimed and reg[0] <= centre(t)[0] <= reg[2] and reg[1] <= centre(t)[1] <= reg[3]]


def edge_trim(s: str) -> str:
    """strip trailing junk but keep the dots an abbreviation ends in ("N.V.D.")"""
    return re.sub(r"[^A-Za-z0-9.]+$", "", s)


def nearest(cands, reg, test):
    cx, cy = (reg[0] + reg[2]) / 2, (reg[1] + reg[3]) / 2
    ok = [(i, t) for i, t in cands if test(edge_trim(t[0]))]
    if not ok:
        return None
    return min(ok, key=lambda it: (centre(it[1])[0] - cx) ** 2 + (centre(it[1])[1] - cy) ** 2)


def read_all(fields: dict[str, TemplateField], tokens: list[Tok], al: Alignment, printed) -> dict[str, str]:
    """Every field of the waybill field set, as the words that fit it."""
    tokens = clean(tokens)
    mask = label_mask(tokens, al, printed)
    claimed: set[int] = set()
    out: dict[str, str] = {}

    def pat(fid, rx, squash=False):
        cands = pool(tokens, mask, claimed, region(al, fields, fid))
        text = join([t for _, t in cands])
        m = re.search(rx, text, flags=re.I | re.S)
        v = m.group(1).strip() if m else ""
        if v:  # the words that made the value are spoken for
            words = {key(w) for w in v.split()}
            claimed.update(i for i, t in cands if key(t[0]) in words)
        return re.sub(r"\s+", "", v) if squash else v

    # most reliable first: each one that is found takes its words out of the pool of the fields that follow
    out["awb_number"] = pat("awb_number", W.AWB_FROM_BOX, True)
    for fid in W.NUMERIC:
        reg = region(al, fields, fid)
        hit = nearest(pool(tokens, mask, claimed, reg), reg, lambda s: bool(re.fullmatch(W.NUMBER, s)))
        out[fid] = edge_trim(hit[1][0]) if hit else ""
        if hit:
            claimed.add(hit[0])
    for fid, rx in W.CODES.items():
        reg = region(al, fields, fid)
        hit = nearest(pool(tokens, mask, claimed, reg), reg, lambda s, rx=rx: bool(re.fullmatch(rx, s)))
        out[fid] = edge_trim(hit[1][0]) if hit else ""
        if hit:
            claimed.add(hit[0])
    out["agent_iata_code"] = pat("agent_iata_code", W.AGENT_IATA_IN_BOX)
    out["executed_date"] = pat("executed_date", W.DATE_IN_BOX)
    out["flight_date"] = pat("flight_date", W.FLIGHT_IN_BOX)
    for fid in W.PLAIN:
        cands = pool(tokens, mask, claimed, region(al, fields, fid))
        out[fid] = join([t for _, t in cands])
        claimed.update(i for i, _ in cands)
    # party cells: the first typed line is the name, the lines below it the address
    for name, addr in (("shipper_name", "shipper_address"), ("consignee_name", "consignee_address")):
        a, b = region(al, fields, name), region(al, fields, addr)
        reg = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
        cands = pool(tokens, mask, claimed, reg)
        ls = lines_of([t for _, t in cands])
        out[name] = " ".join(t[0] for t in ls[0]) if ls else ""
        out[addr] = " ".join(t[0] for l in ls[1:] for t in l)
        claimed.update(i for i, _ in cands)
    cell = join([t for _, t in pool(tokens, mask, claimed, region(al, fields, "nature_of_goods"))])
    m = re.search(W.DESCRIPTION, cell, flags=re.I | re.S)
    out["nature_of_goods"] = m.group(1).strip() if m else ""
    m = re.search(W.HS_CODE, cell, flags=re.I | re.S)
    out["hs_code"] = m.group(1).strip() if m else ""
    m = re.search(W.INVOICE, cell, flags=re.I | re.S)
    out["invoice_reference"] = m.group(1).strip() if m else ""
    return out
