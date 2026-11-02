"""
What the vision-model reader knows about an air waybill, as data: which zone of the page a field belongs to, how far past its
printed cell a value is looked for, the questions put to the model and the patterns the second reader selects words with.

Every constant here was measured on the sixteen real waybills (scripts/proto_select.py, scripts/make_crops.py), and the scripts
now import this module, so the reader that ships and the harness that measured it cannot drift. The regular expressions repeat
scripts/waybill_forms.py (the app image has no scripts/ folder); tests/test_vlm_reader.py pins them equal.
"""

from __future__ import annotations

# --- patterns ----------------------------------------------------------------------------------------------------------

NUMBER = r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,3})?$"
AWB_FROM_BOX = r"(\d{3}\s*-?\s*\d{4}\s*\d{4})"
DECLARED_VALUE = r"^N\.?[VC]\.?[DVC]\.?$"
MONTH = r"(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DAY = r"(?:0?[1-9]|[12]\d|3[01])"
MONTH_NUMBER = r"(?:0?[1-9]|1[0-2])"
DESIGNATOR = r"(?:[A-Z][A-Z0-9]|[0-9][A-Z])"
FLIGHT = rf"^{DESIGNATOR}[- ]?\d{{1,4}}\s?/\s?\d{{1,2}}(?:[A-Z]{{3}})?$"
_MARKER = r"H\.?\s?S\.?[\s-]*CODE|\bINV(?:OICE)?\b|\bDIMS?\b|\bDETAIL\b|\*\*\*"
DESCRIPTION = rf"^\s*(.*?)\s*(?:{_MARKER}|$)"
HS_CODE = r"H\.?\s?S\.?[\s-]*CODE\s*(?:NO\.?)?\s*[:.]?\s*([0-9][0-9 .]{3,12}[0-9])(?!\w)"
INVOICE = r"\bINV(?:OICE)?\b\.?\s*(?:NO\.?)?\s*[:.]?\s*([A-Z0-9][A-Z0-9/\- ]*?)\s*(?:DATE|\bDT\b|DIMS?\b|H\.?\s?S\.?[\s-]*CODE|\bINV(?:OICE)?\b|$)"
DATE_IN_BOX = rf"({DAY}[-/. ]{{0,2}}(?:{MONTH_NUMBER}|{MONTH})[-/. ]{{0,2}}\d{{4}}|{MONTH}\.? ?{DAY} ?,? ?\d{{4}})"
FLIGHT_IN_BOX = r"([A-Z0-9]{2}[- ]?\d{1,4}\s?/\s?\d{1,2}(?:[A-Z]{3})?)"
AGENT_IATA_IN_BOX = r"([\d][\d/\- ]{4,20}\d)"

# --- fields, by zone and kind ------------------------------------------------------------------------------------------

ZONE = {**{f: "top" for f in ("awb_number", "carrier")},
        **{f: "parties" for f in ("shipper_name", "shipper_address", "consignee_name", "consignee_address", "agent_name", "agent_iata_code")},
        **{f: "routing" for f in ("airport_of_departure", "destination_code", "first_carrier_code", "airport_of_destination", "flight_date",
                                   "currency", "charges_code", "declared_value_carriage", "declared_value_customs", "accounting_information")},
        **{f: "table" for f in ("pieces_rcp", "gross_weight", "rate_class", "chargeable_weight", "rate_charge", "total_charge",
                                "nature_of_goods", "hs_code", "invoice_reference")},
        **{f: "charges" for f in ("prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid")},
        **{f: "signing" for f in ("executed_date", "executed_place")}}
FIELD_IDS = frozenset(ZONE)

PARTY = {"shipper": ("shipper_name", "shipper_address"), "consignee": ("consignee_name", "consignee_address")}
GOODS = ("nature_of_goods", "hs_code", "invoice_reference")
SKIP = {f for pair in PARTY.values() for f in pair} | set(GOODS)  # read as a cell with several values, not one crop per field
TABLE_ROW = {"pieces_rcp", "gross_weight", "rate_class", "chargeable_weight", "rate_charge", "total_charge"}

NUMERIC = ("gross_weight", "chargeable_weight", "rate_charge", "total_charge", "prepaid_weight_charge",
           "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid")
CODES = {"destination_code": r"^[A-Z]{3}$", "currency": r"^[A-Z]{3}$", "charges_code": r"^[A-Z]{2}$",
         "rate_class": r"^[A-Z]$", "first_carrier_code": rf"^{DESIGNATOR}$", "pieces_rcp": r"^\d{1,4}$",
         "declared_value_carriage": DECLARED_VALUE, "declared_value_customs": DECLARED_VALUE}
PLAIN = ("carrier", "agent_name", "airport_of_departure", "airport_of_destination", "accounting_information", "executed_place")

# --- search regions ----------------------------------------------------------------------------------------------------

CONTEXT = 0.006  # extra page fraction around a crop's window, so a value cut by its own box is not clipped
SLOP = 0.006  # how far a value may sit outside its own printed cell and still be looked for
# How far past its printed cell a value may be looked for, step by step. Widening is bounded: past 0.04 a neighbouring
# field's value is as likely to be taken as the right one.
LEVELS = (0.0, 0.02, 0.04)

# (expand x, expand y) as page fractions around the aligned box, for the word reader; rows of the cargo table sit lower on drifted scans
EXPAND = {
    **{f: (0.012, 0.030) for f in ("pieces_rcp", "gross_weight", "rate_class", "chargeable_weight", "rate_charge", "total_charge")},
    **{f: (0.015, 0.016) for f in ("prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid")},
}
DEFAULT_EXPAND = (0.012, 0.012)

HINTS = {
    "awb_number": "Give it as the airline's 3-digit prefix, a hyphen, then the 8 digits, with no spaces. Write only digits you can actually see in the image; never fill in digits from memory.",
    "chargeable_weight": "Give only the number, without any 'AS' prefix or unit letter.",
}

# --- the questions put to the model. scripts/gpu/prompts.py is the copy the trainer uses; a test pins the two equal ------------

SINGLE_PROMPT = (
    "You are reading one cell of a scanned air waybill, a shipping form. The cell is for the field '{label}'. "
    "The form's own printed captions in this crop are: {captions!r}. A caption is NEVER the value. Reply with ONLY the typed or "
    "printed value for this field, copied exactly as written, character for character, including any quotation marks. Do not "
    "correct spelling, do not reformat, do not expand abbreviations, do not add words. If the field has several values, give only the first. "
    "If nothing but captions is there, or the value is cut off by the edge of the image, reply EMPTY. Text in a neighbouring cell is not the value. {hint}"
)
PARTY_PROMPT = (
    "You are reading the {label} box of a scanned air waybill. It holds a typed name on the first line and an address on the "
    "lines below it. The form's own printed captions in this crop are: {captions!r}; they are NEVER part of the value. Reply with ONLY this JSON, copying the text exactly as written, no corrections: "
    '{{"name": "<first typed line>", "address": "<remaining typed lines joined with single spaces>"}}'
)
GOODS_PROMPT = (
    "You are reading the 'Nature and Quantity of Goods' cell of a scanned air waybill. The form's own printed captions in this "
    "crop are: {captions!r}; they are NEVER part of the value. Reply with ONLY this JSON, copying text "
    "exactly as written, no corrections: "
    '{{"description": "<the goods description, before any HS code, invoice or dimensions line>", '
    '"hs_code": "<the FIRST HS code only, digits as written, or EMPTY>", "invoice_number": "<a number printed after the word INV or INVOICE, or EMPTY - never a job, HAWB or other reference>"}}'
)


def prompt_for(kind: str, label: str, captions: str, hint: str = "") -> str:
    if kind == "party":
        return PARTY_PROMPT.format(label=label, captions=captions)
    if kind == "goods":
        return GOODS_PROMPT.format(captions=captions)
    return SINGLE_PROMPT.format(label=label, captions=captions, hint=hint)
