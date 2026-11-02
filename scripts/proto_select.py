"""
Prototype (measurement only): read fields by CHOOSING WORDS from one page-level read, instead of OCR-ing a tight crop.

For each field: take the template's box moved by page alignment, widen it to a search region, drop the words that are
the form's own printed labels, and pick the words that fit the field (a number for a weight, three capitals for an
airport code, the first typed line of a party cell for a name). Nothing is learned; nothing is OCR-ed per field.

Uses ONLY the train documents in data/real_awb/split.json; the held-out ones need --final.

usage: python scripts/proto_select.py [--source ocr|native] [--drift DY] [--final] [--show-wrong]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rapidfuzz import fuzz  # noqa: E402

from app.modules import alignment as A  # noqa: E402
from app.modules.vlm_reader import words as _words  # noqa: E402
from app.schemas.template import TemplateLandmark  # noqa: E402
from scripts import waybill_forms as wf  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent / "data" / "real_awb"
# IDP_FORM=M selects the Mumbai/Bangalore-printer template (the reference's family); the default is the Bangkok-printer form
import os as _os  # noqa: E402

FORM = _os.environ.get("IDP_FORM", "B")
if FORM == "M":
    from scripts.seed_templates import AIR_WAYBILL as T  # noqa: E402
else:
    T = wf.AIR_WAYBILL_BKK
FIELDS = {f.id: f for f in T.fields}
# IDP_LANDMARKS=path/to/file.json swaps the landmark list, e.g. one built from training pages only for a held-out evaluation
import os  # noqa: E402

_LM_FILE = os.environ.get("IDP_LANDMARKS")
LM = [TemplateLandmark(**d) for d in json.loads(Path(_LM_FILE).read_text(encoding="utf-8"))] if _LM_FILE else [TemplateLandmark(**d) if isinstance(d, dict) else d for d in T.landmarks]

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


# --- what is printed on the form (so it is never mistaken for a value) -------------------------------------------------


def printed_words(reference_tokens: list[Tok]) -> list[tuple[str, float, float]]:
    """The reference page's words that sit OUTSIDE every marked value box: the form's own labels and boilerplate. This is
    what a user's single marked reference page gives for free - everything they did not mark."""
    out = []
    for t in reference_tokens:
        cx, cy = centre(t)
        if any(f.bbox[0] <= cx <= f.bbox[2] and f.bbox[1] <= cy <= f.bbox[3] for f in T.fields):
            continue
        if key(t[0]):
            out.append((key(t[0]), cx, cy))
    return out


def label_mask(tokens: list[Tok], al: A.Alignment, printed) -> set[int]:
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


# --- search regions ----------------------------------------------------------------------------------------------------

# (expand x, expand y) as page fractions around the aligned box; rows of the cargo table sit lower on drifted scans
EXPAND = {
    **{f: (0.012, 0.030) for f in ("pieces_rcp", "gross_weight", "rate_class", "chargeable_weight", "rate_charge", "total_charge")},
    **{f: (0.015, 0.016) for f in ("prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid")},
}
DEFAULT_EXPAND = (0.012, 0.012)


def region(al, field_id, dx=0.0, dy=0.0):
    b = al.map_box(FIELDS[field_id].bbox)
    ex, ey = EXPAND.get(field_id, DEFAULT_EXPAND)
    return (b[0] - ex + dx, b[1] - ey + dy, b[2] + ex + dx, b[3] + ey + dy)


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


def pool(tokens, mask, claimed, reg):
    """(index, token) of the words inside a region that are neither a printed label nor already taken by another field"""
    return [(i, t) for i, t in enumerate(tokens)
            if i not in mask and i not in claimed and reg[0] <= centre(t)[0] <= reg[2] and reg[1] <= centre(t)[1] <= reg[3]]


# --- choosing the words ------------------------------------------------------------------------------------------------

NUMERIC = ("gross_weight", "chargeable_weight", "rate_charge", "total_charge", "prepaid_weight_charge",
           "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid")
CODES = {"destination_code": r"^[A-Z]{3}$", "currency": r"^[A-Z]{3}$", "charges_code": r"^[A-Z]{2}$",
         "rate_class": r"^[A-Z]$", "first_carrier_code": rf"^{wf.DESIGNATOR}$", "pieces_rcp": r"^\d{1,4}$",
         "declared_value_carriage": wf.DECLARED_VALUE, "declared_value_customs": wf.DECLARED_VALUE}
PLAIN = ("carrier", "agent_name", "airport_of_departure", "airport_of_destination", "accounting_information", "executed_place")


def edge_trim(s: str) -> str:
    """strip trailing junk but keep the dots an abbreviation ends in ("N.V.D.")"""
    return re.sub(r"[^A-Za-z0-9.]+$", "", s)


def nearest(cands, reg, test):
    cx, cy = (reg[0] + reg[2]) / 2, (reg[1] + reg[3]) / 2
    ok = [(i, t) for i, t in cands if test(edge_trim(t[0]))]
    if not ok:
        return None
    return min(ok, key=lambda it: (centre(it[1])[0] - cx) ** 2 + (centre(it[1])[1] - cy) ** 2)


ZONE = {**{f: "top" for f in ("awb_number", "carrier")},
        **{f: "parties" for f in ("shipper_name", "shipper_address", "consignee_name", "consignee_address", "agent_name", "agent_iata_code")},
        **{f: "routing" for f in ("airport_of_departure", "destination_code", "first_carrier_code", "airport_of_destination", "flight_date",
                                   "currency", "charges_code", "declared_value_carriage", "declared_value_customs", "accounting_information")},
        **{f: "table" for f in ("pieces_rcp", "gross_weight", "rate_class", "chargeable_weight", "rate_charge", "total_charge",
                                "nature_of_goods", "hs_code", "invoice_reference")},
        **{f: "charges" for f in ("prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid")},
        **{f: "signing" for f in ("executed_date", "executed_place")}}


def read_all(tokens: list[Tok], al, printed, offsets=None, zones=None) -> dict[str, str]:
    """offsets: zone -> (dx, dy) added to that zone's search regions; zones: read only these zones (None = all).

    With neither, this is app/modules/vlm_reader/words.py - the reader that ships. The offset and zone options are the prototype's
    own (estimate_offsets, below), measured and not adopted, so that older body stays for them."""
    if not offsets and zones is None:
        return _words.read_all(FIELDS, tokens, al, printed)
    offsets = offsets or {}
    tokens = clean(tokens)
    mask = label_mask(tokens, al, printed)
    claimed: set[int] = set()
    out: dict[str, str] = {}

    def off(fid):
        return offsets.get(ZONE[fid], (0.0, 0.0))

    def want(fid):
        return zones is None or ZONE[fid] in zones

    def pat(fid, rx, squash=False):
        cands = pool(tokens, mask, claimed, region(al, fid, *off(fid)))
        text = join([t for _, t in cands])
        m = re.search(rx, text, flags=re.I | re.S)
        v = m.group(1).strip() if m else ""
        if v:  # the words that made the value are spoken for
            words = {key(w) for w in v.split()}
            claimed.update(i for i, t in cands if key(t[0]) in words)
        return re.sub(r"\s+", "", v) if squash else v

    # most reliable first: each one that is found takes its words out of the pool of the fields that follow
    out["awb_number"] = pat("awb_number", wf.AWB_FROM_BOX, True)
    for fid in NUMERIC:
        if not want(fid):
            continue
        reg = region(al, fid, *off(fid))
        hit = nearest(pool(tokens, mask, claimed, reg), reg, lambda s: bool(re.fullmatch(wf.NUMBER, s)))
        out[fid] = edge_trim(hit[1][0]) if hit else ""
        if hit:
            claimed.add(hit[0])
    for fid, rx in CODES.items():
        if not want(fid):
            continue
        reg = region(al, fid, *off(fid))
        hit = nearest(pool(tokens, mask, claimed, reg), reg, lambda s, rx=rx: bool(re.fullmatch(rx, s)))
        out[fid] = edge_trim(hit[1][0]) if hit else ""
        if hit:
            claimed.add(hit[0])
    out["agent_iata_code"] = pat("agent_iata_code", r"([\d][\d/\- ]{4,20}\d)")
    out["executed_date"] = pat("executed_date", rf"({wf._DAY}[-/. ]{{0,2}}(?:{wf._MONTH_NUMBER}|{wf._MONTH})[-/. ]{{0,2}}\d{{4}}|{wf._MONTH}\.? ?{wf._DAY} ?,? ?\d{{4}})")
    out["flight_date"] = pat("flight_date", r"([A-Z0-9]{2}[- ]?\d{1,4}\s?/\s?\d{1,2}(?:[A-Z]{3})?)")
    for fid in PLAIN:
        if not want(fid):
            continue
        cands = pool(tokens, mask, claimed, region(al, fid, *off(fid)))
        out[fid] = join([t for _, t in cands])
        claimed.update(i for i, _ in cands)
    # party cells: the first typed line is the name, the lines below it the address
    for name, addr in (("shipper_name", "shipper_address"), ("consignee_name", "consignee_address")):
        if not want(name):
            continue
        a, b = region(al, name, *off(name)), region(al, addr, *off(addr))
        reg = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
        cands = pool(tokens, mask, claimed, reg)
        ls = lines_of([t for _, t in cands])
        out[name] = " ".join(t[0] for t in ls[0]) if ls else ""
        out[addr] = " ".join(t[0] for l in ls[1:] for t in l)
        claimed.update(i for i, _ in cands)
    cell = join([t for _, t in pool(tokens, mask, claimed, region(al, "nature_of_goods", *off("nature_of_goods")))])
    m = re.search(wf.DESCRIPTION, cell, flags=re.I | re.S)
    out["nature_of_goods"] = m.group(1).strip() if m else ""
    m = re.search(wf.HS_CODE, cell, flags=re.I | re.S)
    out["hs_code"] = m.group(1).strip() if m else ""
    m = re.search(wf.INVOICE, cell, flags=re.I | re.S)
    out["invoice_reference"] = m.group(1).strip() if m else ""
    return out


def _num(v):
    v = v.replace(",", "")
    return float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else None


def consistency(out: dict[str, str], zone: str) -> float:
    """How well a zone's reads agree with the form's own rules - no labels involved."""
    sc = 0.0
    if zone == "top":
        v = out.get("awb_number", "")
        sc += 1.0 * bool(re.fullmatch(wf.AWB, v))
        m = re.fullmatch(r"(\d{3})-(\d{7})(\d)", v)
        sc += 4.0 * bool(m and int(m.group(2)) % 7 == int(m.group(3)))  # the IATA check digit
    elif zone == "table":
        for f, rx in (("pieces_rcp", r"\d{1,4}"), ("gross_weight", wf.NUMBER), ("chargeable_weight", wf.NUMBER), ("rate_charge", wf.NUMBER), ("total_charge", wf.NUMBER), ("rate_class", r"[A-Z]")):
            sc += bool(re.fullmatch(rx, out.get(f, "")))
        c, r, t = _num(out.get("chargeable_weight", "")), _num(out.get("rate_charge", "")), _num(out.get("total_charge", ""))
        sc += 4.0 * bool(c and r and t and abs(c * r - t) <= 0.01)  # weight x rate = total
    elif zone == "charges":
        for f in ("prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid"):
            sc += bool(re.fullmatch(wf.NUMBER, out.get(f, "")))
        a, b, c, t = (_num(out.get(f, "")) for f in ("prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid"))
        if a is not None and t is not None:
            sc += 4.0 * (abs(a + (b or 0) + (c or 0) - t) <= 0.01)  # charges add up to the total (a blank charge counts as nothing)
    elif zone == "routing":
        for f, rx in (("destination_code", r"[A-Z]{3}"), ("first_carrier_code", wf.DESIGNATOR), ("currency", r"[A-Z]{3}"), ("charges_code", r"[A-Z]{2}"),
                      ("flight_date", wf.FLIGHT), ("declared_value_carriage", wf.DECLARED_VALUE), ("declared_value_customs", wf.DECLARED_VALUE)):
            sc += bool(re.fullmatch(rx, out.get(f, "")))
    elif zone == "signing":
        sc += 2.0 * bool(re.fullmatch(wf.DATE, out.get("executed_date", "")))
    return sc


GRID_DY = [i * 0.004 for i in range(-10, 11)]
GRID_DX = [i * 0.008 for i in range(-4, 5)]
SEARCH_ZONES = ("top", "routing", "table", "charges", "signing")


def estimate_offsets(tokens, al, printed) -> dict[str, tuple[float, float]]:
    """Per zone, the (dx, dy) under which the reads are most self-consistent; the smallest shift wins a tie."""
    best = {}
    for zone in SEARCH_ZONES:
        scored = []
        for dy in GRID_DY:
            for dx in GRID_DX:
                out = read_all(tokens, al, printed, {zone: (dx, dy)}, zones={zone})
                scored.append((consistency(out, zone), -(abs(dx) + abs(dy)), dx, dy))
        top = max(scored)
        best[zone] = (top[2], top[3]) if top[0] > 0 else (0.0, 0.0)
    return best


# --- driver --------------------------------------------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="ocr", choices=["ocr", "native"], help="word source for documents that have a text layer")
    ap.add_argument("--drift", type=float, default=0.0, help="shift every typed (non-printed) native word down by up to this much")
    ap.add_argument("--final", action="store_true", help="evaluate the held-out documents (once, at the end)")
    ap.add_argument("--show-wrong", action="store_true")
    ap.add_argument("--offset", action="store_true", help="estimate the per-zone print offset of each document")
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()

    split = json.loads((ROOT / "split.json").read_text(encoding="utf-8"))["templates"][{"B": "B_bangkok_printer", "M": "M_mumbai_printer"}[FORM]]
    wanted = set(split["test"] if args.final else split["train"])
    tokens_all = json.loads((ROOT / "work" / "tokens.json").read_text(encoding="utf-8"))
    ref_tokens = tokens_all[{"B": "08_176021952812_emirates.json", "M": "09_176060677362_reference.json"}[FORM]]["native"]
    printed = printed_words(ref_tokens)
    rnd = random.Random(5)

    per_field = defaultdict(Counter)
    tot = Counter()
    for name, rec in tokens_all.items():
        num = name[:2]
        if num not in wanted or (args.only and num not in args.only):
            continue
        lab = json.loads((ROOT / "labels" / name).read_text(encoding="utf-8"))["fields"]
        toks = rec["native"] if (rec["native"] and args.source == "native") else rec["ocr"]
        toks = [tuple(t) for t in toks]
        if args.drift and rec["native"]:
            # simulate a misregistered print: typed words move, printed ones stay
            printed_keys = {(k, round(x, 2), round(y, 2)) for k, x, y in printed}
            moved = []
            ddy, ddx = rnd.uniform(-args.drift, args.drift), rnd.uniform(-args.drift / 2, args.drift / 2)
            for t in toks:
                cx, cy = centre(t)
                if (key(t[0]), round(cx, 2), round(cy, 2)) in printed_keys:
                    moved.append(t)
                else:
                    moved.append((t[0], t[1] + ddx, t[2] + ddy, t[3] + ddx, t[4] + ddy, t[5]))
            toks = moved
        words = [A.PageWord(key(t[0]), t[1], (t[2] + t[4]) / 2) for t in toks if re.fullmatch(r"[A-Za-z]{4,}", re.sub(r"[^A-Za-z]", "", t[0])) and len(re.sub(r"[^A-Za-z]", "", t[0])) >= 4 and t[5] >= 30]
        al = A.fit_alignment(LM, words)
        if al is None:
            print(f"{name}: no alignment"); continue
        offs = estimate_offsets(toks, al, printed) if args.offset else {}
        got = read_all(toks, al, printed, offs)
        right = 0
        wrong = []
        for fid, want in lab.items():
            ok = norm(got.get(fid, "")) == norm(want)
            right += ok
            per_field[fid]["right" if ok else "wrong"] += 1
            if not ok:
                wrong.append((fid, norm(want)[:44], norm(got.get(fid, ""))[:44]))
        tot["right"] += right
        tot["n"] += len(lab)
        if offs:
            print("   offsets", {z: (round(o[0], 3), round(o[1], 3)) for z, o in offs.items()})
        print(f"{name:40s} {'native' if rec['native'] else 'scan  '} source={'native' if (rec['native'] and args.source == 'native') else 'ocr':6s} landmarks {al.landmarks:3d}  right {right}/{len(lab)}")
        if args.show_wrong:
            for fid, want, g in wrong:
                print(f"       {fid:28s} want {want!r:48s} got {g!r}")
    print(f"\nTOTAL {tot['right']}/{tot['n']} right over the {'TEST' if args.final else 'train'} documents")
    print("\nper field:", "  ".join(f"{k}={v['right']}/{v['right'] + v['wrong']}" for k, v in per_field.items()))


if __name__ == "__main__":
    main()
