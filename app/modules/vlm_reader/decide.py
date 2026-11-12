"""
What to make of the vision model's answers: which window's answer a field takes, and whether it may be trusted.

Pure functions over strings and template fields: no model, no image, no I/O. scripts/score_vlm.py imports these, so the rule that
ships is the rule that was measured.

The model reads every crop at several windows (waybill.LEVELS: the printed cell, then wider). A field's answer is STAGED:

  take the tightest window whose answer passes the field's format;
  EXTENSION: a wider window's format-valid answer that strictly CONTAINS the chosen one replaces it (the tighter crop was clipped:
             1432.0 for 1432.00, 5.00 for 5.000 - the model copied what was visible and the clipped value still passed its format);
  BLANK STAYS BLANK: when the tightest window said EMPTY, a value found only by widening is kept only if the later windows agree on
             it. Windows that disagree (7,430.00 then 500.00) are reading different neighbouring cells.
  WIDER WINDOWS AGREE: when the two widest windows give the same format-valid answer and the pick differs from it, take that answer.
             The tightest window is cut from the box the page alignment predicts, and a page the alignment fits loosely (a tilt it
             has no term for, a row a little lower) leaves that window on the NEIGHBOURING cell, which then reads a well-formed wrong
             value (173.00 for 47,276.00, THAILAND for BKK) while both wider windows, which reach the real cell, agree on the right one.
             Measured on the saved replies of 4,739 synthetic validation fields (two forms, four sets) it fixed 29 and broke 6
             (right 4,485 -> 4,508, wrong non-empty 202 -> 179); on the five real held-out pages 5 and 1. Of the 6 it broke, 2 were
             spacing or punctuation differences between windows and 4 were wider windows that both misread a digit (976 for 376).

All of them only ever choose between the model's own answers, never edit one.

A staged value is GATED (the only kind that may ever be auto-approved) when a second, independent reading agrees with it - the
word-selection reader on the page's own OCR (words.py) - or an arithmetic fact of the document holds over it (AWB check digit,
weight x rate = total, charges add up), AND it passes the field's format. Window agreement is NOT a gate: errors across windows
are correlated (measured: 3-8 wrong per 500 fields passed it).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from app.modules.field_extraction import _select
from app.schemas.template import TemplateField


def norm(s: str) -> str:
    return " ".join((s or "").split())


def parse_json(reply: str) -> dict:
    m = re.search(r"\{.*\}", reply, flags=re.S)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def value_of(reply: str) -> str:
    """One answer as a value. Quotation marks are kept: '"FREIGHT PREPAID"' is the printed value."""
    v = reply.strip().strip("`").strip()
    return "" if v.upper() in ("EMPTY", "NONE", "N/A", "") else v


def answer_fields(kind: str, fields: list[str], reply: str) -> dict[str, str]:
    """The reply to one crop, as the value of each field the crop asked about."""
    if kind == "single":
        return {fields[0]: value_of(reply)}
    j = parse_json(reply)
    if kind == "party":
        return {fields[0]: value_of(str(j.get("name", ""))), fields[1]: value_of(str(j.get("address", "")))}
    return {"nature_of_goods": value_of(str(j.get("description", ""))), "hs_code": value_of(str(j.get("hs_code", ""))),
            "invoice_reference": value_of(str(j.get("invoice_number", "")))}


def _num(v: str) -> Optional[float]:
    v = (v or "").replace(",", "")
    return float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else None


def checked_fields(vals: dict[str, str]) -> set[str]:
    """Fields that take part in an arithmetic check of the document that PASSES: the AWB check digit, weight x rate = total,
    and weight charge + agent + carrier charges = total prepaid. A misread digit almost never keeps these true."""
    ok: set[str] = set()
    m = re.fullmatch(r"(\d{3})-(\d{7})(\d)", vals.get("awb_number", ""))
    if m and int(m.group(2)) % 7 == int(m.group(3)):
        ok.add("awb_number")
    c, r, t = (_num(vals.get(f, "")) for f in ("chargeable_weight", "rate_charge", "total_charge"))
    if c and r and t and abs(c * r - t) <= 0.01:
        ok |= {"chargeable_weight", "rate_charge", "total_charge"}
    a, b, d, tot = (_num(vals.get(f, "")) for f in ("prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid"))
    if a is not None and tot is not None and abs(a + (b or 0) + (d or 0) - tot) <= 0.01:
        ok |= {"prepaid_weight_charge", "total_prepaid"} | ({"total_other_charges_agent"} if b is not None else set()) | ({"total_other_charges_carrier"} if d is not None else set())
    return ok


def passes_format(field: TemplateField, v: str) -> bool:
    rx = field.validation_regex
    return bool(v) and (rx is None or bool(re.fullmatch(rx, v)))


def selected(field: TemplateField, raw: str) -> str:
    """The value after the template's own declared selection (AWB squash, first number ...), as Module 6 applies it."""
    if raw and (field.extract_pattern or field.squash_spaces) and field.from_field is None:
        return _select(field, raw)[0]
    return raw


def staged(field: TemplateField, per_level: dict[int, str]) -> tuple[str, Optional[int], bool]:
    """(value, level it came from, passed its format)."""
    levels = sorted(per_level)
    values = {lv: selected(field, per_level[lv]) for lv in levels}
    pick: tuple[str, Optional[int], bool] = ("", None, False)
    for lv in levels:
        if passes_format(field, values[lv]):
            pick = (values[lv], lv, True)
            break
    else:
        for lv in levels:
            if values[lv]:
                pick = (values[lv], lv, False)
                break
    value, level, valid = pick
    if not value:
        return pick
    for wider in levels:
        v = norm(values[wider])
        if wider > level and v != norm(value) and len(v) > len(norm(value)) and norm(value) in v and passes_format(field, v):
            value, level, valid = values[wider], wider, True
            break
    if len(levels) >= 3:
        w1, w2 = values[levels[1]], values[levels[2]]
        if w1 and norm(w1) == norm(w2) and passes_format(field, w1) and norm(w1) != norm(value):
            value, level, valid = w1, levels[1], True
    if not norm(values[levels[0]]):  # the tightest window said EMPTY, so `value` came from widening
        later = {norm(values[lv]) for lv in levels if lv > 0 and norm(values[lv])}
        if len(later) > 1:
            return "", None, False
    return value, level, valid


@dataclass(frozen=True)
class Decision:
    value: str
    level: Optional[int]  # the window the value came from; None = no usable answer
    valid: bool  # passes the field's format
    agree: bool  # the word reader read the same value
    checked: bool  # an arithmetic check of the document holds over it
    gated: bool  # may be auto-approved: (agree or checked) and valid


def decide(
    fields: dict[str, TemplateField], answers: dict[str, dict[int, str]], words: dict[str, str]
) -> dict[str, Decision]:
    """Every field's decision. `answers`: field -> window level -> raw model answer. `words`: the word reader's value per field.
    A field with no answer at all is a blank, never gated."""
    staged_by = {fid: staged(f, answers[fid]) for fid, f in fields.items() if fid in answers}
    corroborated = checked_fields({fid: s[0] for fid, s in staged_by.items()})
    out: dict[str, Decision] = {}
    for fid, field in fields.items():
        value, level, valid = staged_by.get(fid, ("", None, False))
        agree = bool(value) and norm(value) == norm(words.get(fid, ""))
        checked = fid in corroborated
        out[fid] = Decision(value, level, valid, agree, checked, gated=(agree or checked) and passes_format(field, value))
    return out
