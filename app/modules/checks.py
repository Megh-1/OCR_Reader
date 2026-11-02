"""
Cross-field checks (part of Module 7).

Pure functions: no database, no OCR, no I/O. `evaluate_checks` takes a template's checks and the
values that were read and says, per check, passed / failed / skipped. validation.validate_fields
turns every failure into extra caution on the fields involved.

A check can only ADD caution. It never edits a value, never promotes a flagged field, and is
skipped (not passed) whenever it cannot decide: an input missing, empty, unparseable, or a key
the table does not know. Malformed inputs are the field's own format check to report, so a skip
neither hides nor duplicates that finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from app.schemas.template import TemplateCheck

# Active ISO 4217 alphabetic codes. A code missing here is flagged for a person to look at,
# which is the safe direction; add codes as real documents need them.
ISO_4217 = frozenset(
    "AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD "
    "CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD "
    "GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT "
    "LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR "
    "NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP "
    "STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF "
    "XPF YER ZAR ZMW ZWL".split()
)
LOOKUPS = {"iso4217": ISO_4217}

_NUMBER = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


@dataclass
class CheckOutcome:
    check_id: str
    label: str
    status: Literal["passed", "failed", "skipped"]
    detail: str
    fields: list[str]  # the fields to flag when status is "failed"


def _number(text: str) -> Optional[Decimal]:
    text = text.strip()
    if not _NUMBER.match(text):
        return None
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _read(check: TemplateCheck, field_id: str, values: dict[str, str]) -> Optional[str]:
    """The field's value, narrowed by its pattern if the check gave one. None = cannot be used."""
    raw = values.get(field_id)
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    pattern = check.patterns.get(field_id)
    if pattern is None:
        return raw
    match = re.search(pattern, raw)
    return match.group(1).strip() if match and match.group(1).strip() else None


def _outcome(check: TemplateCheck, status: str, detail: str) -> CheckOutcome:
    return CheckOutcome(check.id, check.label, status, detail, check.field_ids())  # type: ignore[arg-type]


def _evaluate(check: TemplateCheck, values: dict[str, str]) -> CheckOutcome:
    if check.when is not None:
        gate = values.get(check.when.field, "").strip().upper()
        if gate != check.when.equals.strip().upper():
            return _outcome(check, "skipped", f"{check.when.field} is not {check.when.equals}")

    read = [_read(check, f, values) for f in check.inputs]
    if any(v is None for v in read):
        return _outcome(check, "skipped", "an input is missing or does not match its pattern")

    if check.kind in ("product", "sum"):
        nums = [_number(v) for v in read]
        target = _number(values.get(check.target or "", ""))
        if target is None or any(n is None for n in nums):
            return _outcome(check, "skipped", "an input is not a number")
        if check.kind == "product":
            expected = nums[0] * nums[1]
        else:
            expected = sum(nums, Decimal(0))
        ok = abs(expected - target) <= Decimal(str(check.tolerance))
        return _outcome(check, "passed" if ok else "failed", f"expected {expected}, read {target}")

    if check.kind == "equal":
        a, b = read[0].upper(), read[1].upper()
        return _outcome(check, "passed" if a == b else "failed", f"{a!r} vs {b!r}")

    if check.kind == "one_of":
        allowed = {v.upper() for v in check.values} | (LOOKUPS[check.lookup] if check.lookup else set())
        ok = read[0].upper() in allowed
        return _outcome(check, "passed" if ok else "failed", f"{read[0]!r} {'is' if ok else 'is not'} an allowed value")

    # maps_to
    table = {k.upper(): v for k, v in check.table.items()}
    key = read[0].upper()
    if key not in table:
        return _outcome(check, "skipped", f"{key!r} is not in the table")
    target = (values.get(check.target or "") or "").strip()
    if not target:
        return _outcome(check, "skipped", "the target field is empty")
    ok = target.upper() == table[key].upper()
    return _outcome(check, "passed" if ok else "failed", f"{key!r} should give {table[key]!r}, read {target!r}")


def evaluate_checks(checks: list[TemplateCheck], values: dict[str, str]) -> list[CheckOutcome]:
    return [_evaluate(check, values) for check in checks]


# --------------------------------------------------------------------------- suggestions


def _check_id(kind: str, ids: list[str]) -> str:
    return re.sub(r"[^a-z0-9_]", "_", f"{kind}_{'_'.join(ids)}".lower())[:60].rstrip("_")


def suggest_checks(
    documents: list[dict[str, str]], labels: dict[str, str] | None = None, min_documents: int = 2, max_sum_inputs: int = 3
) -> list[dict]:
    """
    Rules a person might want, found by looking at what a form's fields actually hold across several
    documents already read. Pure and deterministic; it only PROPOSES - nothing is saved until a person
    adds it, and the person sees on how many documents each held.

    A relation is proposed only if it held on EVERY document where all its fields could be read, and
    on at least `min_documents` of them with non-degenerate values (a product with a factor of 0 or 1,
    a sum with a zero term, or two fields that always hold the same constant say nothing). One
    document is never enough: 3 x 4 = 12 is also 6 x 2.

      product / sum   numeric fields (a thousands comma or none, then a decimal point)
      equal           two fields that hold the same text on every document, with varying text
      one_of          a field whose values are all ISO 4217 currency codes
    """
    labels = labels or {}
    field_ids = sorted({f for d in documents for f in d})
    numbers = {f: [(_number(d[f]) if f in d and d[f].strip() else None) for d in documents] for f in field_ids}
    numeric = [f for f in field_ids if sum(v is not None for v in numbers[f]) >= min_documents]
    out: list[dict] = []

    def name(f: str) -> str:
        return labels.get(f) or f

    def usable(rows: list[tuple]) -> bool:
        return len(rows) >= min_documents

    # product: a * b == c
    for c in numeric:
        for i, a in enumerate(numeric):
            for b in numeric[i + 1:]:
                if c in (a, b):
                    continue
                rows = [(numbers[a][k], numbers[b][k], numbers[c][k]) for k in range(len(documents))
                        if None not in (numbers[a][k], numbers[b][k], numbers[c][k])]
                if not usable(rows) or any(abs(x * y - z) > Decimal("0.01") for x, y, z in rows):
                    continue
                if sum(1 for x, y, _ in rows if x not in (0, 1) and y not in (0, 1)) < min_documents:
                    continue
                out.append({"id": _check_id("product", [a, b, c]), "label": f"{name(a)} x {name(b)} = {name(c)}", "kind": "product",
                            "inputs": [a, b], "target": c, "tolerance": 0.01, "documents": len(rows)})

    # sum: a + b (+ d) == c
    from itertools import combinations
    for c in numeric:
        others = [f for f in numeric if f != c]
        for size in range(2, max_sum_inputs + 1):
            for inputs in combinations(others, size):
                rows = [([numbers[f][k] for f in inputs], numbers[c][k]) for k in range(len(documents))
                        if numbers[c][k] is not None and all(numbers[f][k] is not None for f in inputs)]
                if not usable(rows) or any(abs(sum(vals, Decimal(0)) - z) > Decimal("0.01") for vals, z in rows):
                    continue
                if sum(1 for vals, _ in rows if all(v != 0 for v in vals)) < min_documents:
                    continue
                # a sum that merely contains a smaller proposed sum adds nothing: keep the smallest
                if any(set(o["inputs"]) < set(inputs) and o["kind"] == "sum" and o["target"] == c for o in out):
                    continue
                out.append({"id": _check_id("sum", [*inputs, c]), "label": f"{' + '.join(name(f) for f in inputs)} = {name(c)}", "kind": "sum",
                            "inputs": list(inputs), "target": c, "tolerance": 0.01, "documents": len(rows)})

    # equal: the same text in two fields, on every document, and not always the same constant
    texts = {f: [(d.get(f) or "").strip() for d in documents] for f in field_ids}
    for i, a in enumerate(field_ids):
        for b in field_ids[i + 1:]:
            rows = [(texts[a][k], texts[b][k]) for k in range(len(documents)) if texts[a][k] and texts[b][k]]
            if usable(rows) and all(x.upper() == y.upper() for x, y in rows) and len({x.upper() for x, _ in rows}) >= min_documents:
                out.append({"id": _check_id("equal", [a, b]), "label": f"{name(a)} agrees with {name(b)}", "kind": "equal",
                            "inputs": [a, b], "documents": len(rows)})

    # one_of: every value is a currency code
    for f in field_ids:
        vals = [v for v in texts[f] if v]
        if len(vals) >= min_documents and all(v.upper() in ISO_4217 for v in vals):
            out.append({"id": _check_id("one_of", [f, "iso4217"]), "label": f"{name(f)} is a currency code", "kind": "one_of",
                        "inputs": [f], "lookup": "iso4217", "documents": len(vals)})

    seen, unique = set(), []
    for item in out:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    return unique
