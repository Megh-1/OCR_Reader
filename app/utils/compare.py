"""How two readings of one field are compared (the sprint's scorer and the agreement check use exactly this, so a number and a tier mean the same).

strip, collapse whitespace, case-insensitive; numeric fields compared as numbers after removing thousands commas (1,234.00 == 1234);
hs_code / invoice_reference ignore a leading printed label. Nothing else is forgiven.
"""

from __future__ import annotations

import re

NUMERIC_FIELDS = frozenset({
    "pieces_rcp", "gross_weight", "chargeable_weight", "rate_charge", "total_charge", "prepaid_weight_charge",
    "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid",
})

_LEADING_LABEL = {
    "hs_code": re.compile(r"^\s*HS\s*CODE\s*(NO\.?)?\s*:?\s*", re.I),
    "invoice_reference": re.compile(r"^\s*INV(OICE)?\s*(NO\.?)?\s*:?\s*", re.I),
}
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


def normalise(field_id: str, value) -> str:
    text = "" if value is None else str(value)
    label = _LEADING_LABEL.get(field_id)
    if label:
        text = label.sub("", text)
    return " ".join(text.strip().lower().split())


def values_agree(field_id: str, a, b) -> bool:
    x, y = normalise(field_id, a), normalise(field_id, b)
    if x == y:
        return True
    if field_id in NUMERIC_FIELDS:
        x2, y2 = x.replace(",", ""), y.replace(",", "")
        if _NUMBER.match(x2) and _NUMBER.match(y2):
            return float(x2) == float(y2)
    return False
