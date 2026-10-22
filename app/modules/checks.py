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
