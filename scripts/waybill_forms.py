"""
Air waybill layouts beyond the reference (`air_waybill`, cut from one Bangalore PDF).

Sixteen real waybills from different printers showed that "an air waybill" is several printed
forms, and a template is only as general as the form its boxes were cut from:

  air_waybill_bkk   the form the Bangkok agents' system prints (11 of the 16 files): shipper at the top
                    left, "BANGKOK THAILAND" under the airline, "Declared Vale for Carriage" misspelt.
                    Boxes cut from the rule geometry of two text-native files that share it exactly.

Two things differ from the reference template and are deliberate:

  * Boxes span the whole printed CELL, not the reference document's own value. A value in a table
    cell is right-aligned, so its position moves with its width, and a box cut tight around one
    document's value missed "83.320" by 0.04 of the page on another.
  * The goods cell is read ONCE and the description, HS code and invoice number are selected out of
    its text (TemplateField.from_field / extract_pattern). Those three sit on lines that move from
    document to document, so no box per line can work.

Money and weights use a strict pattern: a thousands comma or none, then a decimal POINT. The
reference's `^[\\d.,]{1,12}$` accepted "642635,35" for "642635.35".

Every figure that decides a box below was measured on those files; the label files they are checked
against are in data/real_awb/labels (untracked, real third-party data).
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.template import TemplateCheck, TemplateIn


def landmarks(name: str) -> list[dict]:
    """Printed-word positions for page alignment (app/modules/alignment.py); missing file = none."""
    path = Path(__file__).resolve().parent / "landmarks" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


FLOOR = 0.60

# "65,000.00", "11050", "83.320", "1,432.00": a thousands comma or none, then a decimal point.
NUMBER = r"^(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,3})?$"
# The first number in a cell that may print a prefix ("AS 500.00") or a unit ("76.00 K.") around it.
FIRST_NUMBER = r"(\d[\d.,]*\d|\d)"
AWB = r"^\d{3}-\d{8}$"
AWB_FROM_BOX = r"(\d{3}\s*-?\s*\d{4}\s*\d{4})"  # "176-0669 1322", "205 - 6129 0526", "176-06067736"
AGENT_IATA = r"^[\d/\- ]{6,22}$"
# "No value declared" for carriage or customs: NVD, N.V.D., N.V.D, and NCV / NVC, which real forms also print.
DECLARED_VALUE = r"^N\.?[VC]\.?[DVC]\.?$"
# Month NAMES are enumerated in full (a trailing [a-z]* let "Janvary" through, and sat outside the case-insensitive
# group so that "JANUARY" in capitals did not match at all).
_MONTH = r"(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
# Day and month are as strict as the calendar: a shape-only \d{1,2} let "28/13/2025" through, and an OCR slip
# that lands on a real-looking date is exactly what the pattern should still be able to refuse.
_DAY = r"(?:0?[1-9]|[12]\d|3[01])"
_MONTH_NUMBER = r"(?:0?[1-9]|1[0-2])"
DATE = rf"^(?:{_DAY}[-/. ]{{0,2}}(?:{_MONTH_NUMBER}|{_MONTH})[-/. ]{{0,2}}\d{{4}}|{_MONTH}\.? ?{_DAY} ?,? ?\d{{4}})$"
PLACE = r"^[A-Za-z][A-Za-z /,.]{1,38}$"
# An airline designator is two characters with at least one letter (EK, G9, 6E); two digits ("37") are not one.
DESIGNATOR = r"(?:[A-Z][A-Z0-9]|[0-9][A-Z])"
FLIGHT = rf"^{DESIGNATOR}[- ]?\d{{1,4}}\s?/\s?\d{{1,2}}(?:[A-Z]{{3}})?$"  # EK375/30, AF195/30JAN, G9817/30

# Pulling the three things that share the goods cell out of its text. The description is whatever comes
# before the first marker of the other two (or a dimensions line); each marker is the printed word.
_MARKER = r"H\.?\s?S\.?[\s-]*CODE|\bINV(?:OICE)?\b|\bDIMS?\b|\bDETAIL\b|\*\*\*"
DESCRIPTION = rf"^\s*(.*?)\s*(?:{_MARKER}|$)"
# The number must END where its digits end: "6109 9O2O" (a misread 0) captured "6109 9", which the validator below
# accepted as a short but well-formed code. (?!\w) refuses a capture that stops short of its own word.
HS_CODE = r"H\.?\s?S\.?[\s-]*CODE\s*(?:NO\.?)?\s*[:.]?\s*([0-9][0-9 .]{3,12}[0-9])(?!\w)"
HS_CODE_VALID = r"^\d{4}[\d. ]{2,9}$"
# \b after INV(OICE): without it "INVERTER" and "INVENTORY" read as an invoice marker followed by "ERTER ...".
INVOICE = r"\bINV(?:OICE)?\b\.?\s*(?:NO\.?)?\s*[:.]?\s*([A-Z0-9][A-Z0-9/\- ]*?)\s*(?:DATE|\bDT\b|DIMS?\b|H\.?\s?S\.?[\s-]*CODE|\bINV(?:OICE)?\b|$)"

# Airline prefix -> the carrier name as these forms print it. Each pair is one the labelled documents
# confirm; a prefix not listed is skipped, never failed (checks.py).
CARRIER_BY_PREFIX = {"176": "EMIRATES", "205": "ALL NIPPON AIRWAYS", "057": "AIR FRANCE", "514": "AIR ARABIA", "217": "THAI"}


def _field(id, label, bbox, regex=None, *, enum=False, **extra):
    return {
        "id": id, "label": label, "bbox": list(bbox), "data_type": "enum" if enum else "string",
        "validation_regex": regex, "confidence_floor": FLOOR, **extra,
    }


def checks() -> list[TemplateCheck]:
    return [
        # Rating: chargeable weight x rate = total. Only for class Q (normal rate); a minimum charge (M), a
        # specific commodity rate (C) and others are not a plain product.
        TemplateCheck(
            id="total_equals_weight_times_rate", label="Total charge = chargeable weight x rate",
            kind="product", inputs=["chargeable_weight", "rate_charge"], target="total_charge",
            tolerance=0.01, when={"field": "rate_class", "equals": "Q"},
        ),
        # Held on every one of the 11 labelled waybills that print all four figures: the weight charge plus
        # what is due the agent and the carrier is the total prepaid. A blank or non-numeric input skips it.
        TemplateCheck(
            id="total_prepaid_is_the_sum", label="Total prepaid = weight charge + due agent + due carrier",
            kind="sum", inputs=["prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier"],
            target="total_prepaid", tolerance=0.01,
        ),
        TemplateCheck(
            id="flight_prefix_matches_first_carrier", label="Flight prefix = first carrier code",
            kind="equal", inputs=["flight_date", "first_carrier_code"],
            patterns={"flight_date": r"^([A-Z0-9]{2})[- ]?\d{1,4}\s?/"},
        ),
        TemplateCheck(id="currency_is_iso_4217", label="Currency is a real ISO 4217 code", kind="one_of", inputs=["currency"], lookup="iso4217"),
        TemplateCheck(
            id="awb_prefix_matches_carrier", label="AWB prefix matches the carrier",
            kind="maps_to", inputs=["awb_number"], target="carrier",
            patterns={"awb_number": r"^(\d{3})"}, table=CARRIER_BY_PREFIX,
        ),
    ]


# --- air_waybill_bkk -------------------------------------------------------------------------------
# Page frame: A4 text-native, 595.44 x 841.68 pt. Rows and columns are the printed rules of that page.
AIR_WAYBILL_BKK = TemplateIn(
    template_id="air_waybill_bkk",
    hints={"page_aspect_ratio": 595.44 / 841.68, "ruled_form": True},
    landmarks=landmarks("air_waybill_bkk"),
    checks=checks(),
    anchors=[
        # Printed by this form's system under every airline name, and the one label that tells this
        # form from the others: it scored 59-100 on the nine Bangkok-form files and 0-45 on the other
        # seven (a different printer's form, Pioneer's own, SalamAir's). It is tiny, so scans read it
        # low, which is why the threshold is 0.55 and not 0.70 - the gap to the other forms is what matters.
        {"text": "BANGKOK THAILAND", "bbox": [0.828, 0.0835, 0.962, 0.0940], "match_threshold": 0.55},
        # A label typed values never land on (the first attempt, "Declared Vale for Carriage", is
        # overprinted by its own value on scans and read 0-91). It is shared with other forms, so it
        # proves "an IATA waybill"; the anchor above proves "this one". Far from it, so the crops cannot bleed.
        {"text": "Shipper's account Number", "bbox": [0.3050, 0.0180, 0.4400, 0.0275], "match_threshold": 0.65},
    ],
    fields=[
        # --- identity: the number printed top right, above the form ---------------------------------
        _field("awb_number", "AWB Number", (0.620, 0.0005, 0.980, 0.0178), AWB, extract_pattern=AWB_FROM_BOX, squash_spaces=True,
               checksum="iata_awb_mod7"),
        # The airline's name sits in the cell's right half; "Not negotiable / Air Waybill / Issued by" are the
        # labels left of x=0.57 and "BANGKOK THAILAND" is the line below y=0.083.
        _field("carrier", "Carrier", (0.570, 0.0195, 0.960, 0.0820), r"^[A-Za-z][A-Za-z .&-]{2,40}$"),

        # --- parties: name is the first typed line, address the rest ---------------------------------
        _field("shipper_name", "Shipper Name", (0.030, 0.0460, 0.474, 0.0590)),
        _field("shipper_address", "Shipper Address", (0.030, 0.0590, 0.474, 0.1050)),
        _field("consignee_name", "Consignee Name", (0.030, 0.1275, 0.474, 0.1395)),
        _field("consignee_address", "Consignee Address", (0.030, 0.1395, 0.474, 0.1920)),
        _field("agent_name", "Issuing Agent", (0.030, 0.2100, 0.474, 0.2400)),
        _field("agent_iata_code", "Agent IATA Code", (0.030, 0.2515, 0.2465, 0.2665), AGENT_IATA),

        # --- routing ----------------------------------------------------------------------------------
        _field("airport_of_departure", "Airport of Departure", (0.030, 0.2780, 0.474, 0.2940)),
        _field("destination_code", "Destination Code", (0.0285, 0.3050, 0.0745, 0.3205), r"^[A-Z]{3}$"),
        _field("first_carrier_code", "First Carrier", (0.0750, 0.3050, 0.3515, 0.3205), rf"^{DESIGNATOR}$"),
        _field("airport_of_destination", "Airport of Destination", (0.030, 0.3325, 0.2560, 0.3478), r"^[A-Za-z][A-Za-z ,.()\-]{2,40}$"),
        _field("flight_date", "Flight / Date", (0.2565, 0.3325, 0.3600, 0.3478), FLIGHT),

        # --- charge declarations ----------------------------------------------------------------------
        _field("currency", "Currency", (0.4755, 0.3050, 0.5415, 0.3205), r"^[A-Z]{3}$"),
        _field("charges_code", "Charges Code", (0.5420, 0.3050, 0.5800, 0.3205), r"^[A-Z]{2}$"),
        _field("declared_value_carriage", "Declared Value for Carriage", (0.6945, 0.3050, 0.8265, 0.3205), DECLARED_VALUE),
        _field("declared_value_customs", "Declared Value for Customs", (0.8275, 0.3050, 0.9610, 0.3205), DECLARED_VALUE),
        _field("accounting_information", "Accounting Information", (0.478, 0.2115, 0.960, 0.2665)),

        # --- cargo line: the first row under the column headings, one column per box ------------------
        _field("pieces_rcp", "No. of Pieces (RCP)", (0.0285, 0.4150, 0.0780, 0.4350), r"^\d{1,4}$"),
        _field("gross_weight", "Gross Weight", (0.0790, 0.4150, 0.1490, 0.4350), NUMBER),
        _field("rate_class", "Rate Class", (0.1795, 0.4150, 0.1985, 0.4350), r"^[A-Z]$", enum=True),
        # "AS 500.00" (a prefix some of these forms print) and "76.00 K." (a unit): the number is the value.
        _field("chargeable_weight", "Chargeable Weight", (0.2935, 0.4150, 0.4135, 0.4350), NUMBER, extract_pattern=FIRST_NUMBER),
        _field("rate_charge", "Rate / Charge", (0.4235, 0.4150, 0.5485, 0.4350), NUMBER),
        _field("total_charge", "Total", (0.5615, 0.4150, 0.6865, 0.4350), NUMBER),
        # The whole goods cell, read once. This field keeps the description; the next two are selected from the
        # same read. Neither the HS code nor an invoice is on every waybill, so both may be absent.
        _field("nature_of_goods", "Nature and Quantity of Goods", (0.6970, 0.4150, 0.9610, 0.6150), extract_pattern=DESCRIPTION),
        _field("hs_code", "HS Code", (0.6970, 0.4150, 0.9610, 0.6150), HS_CODE_VALID,
               from_field="nature_of_goods", extract_pattern=HS_CODE, optional=True),
        _field("invoice_reference", "Invoice Reference", (0.6970, 0.4150, 0.9610, 0.6150), r"^[A-Z0-9][A-Z0-9/\- ]{3,25}$",
               from_field="nature_of_goods", extract_pattern=INVOICE, optional=True),

        # --- totals: the value sits just under its printed label, left of it --------------------------
        _field("prepaid_weight_charge", "Prepaid Weight Charge", (0.0285, 0.6285, 0.1950, 0.6420), NUMBER),
        _field("total_other_charges_agent", "Total Other Charges Due Agent", (0.0285, 0.7085, 0.1750, 0.7240), NUMBER),
        _field("total_other_charges_carrier", "Total Other Charges Due Carrier", (0.0285, 0.7560, 0.1750, 0.7705), NUMBER),
        _field("total_prepaid", "Total Prepaid", (0.0285, 0.7825, 0.2465, 0.7965), NUMBER),

        # --- execution ---------------------------------------------------------------------------------
        _field("executed_date", "Executed On (Date)", (0.4755, 0.7940, 0.5950, 0.8120), DATE),
        _field("executed_place", "Executed At (Place)", (0.5950, 0.7940, 0.7800, 0.8120), PLACE),
    ],
)

ALL = [AIR_WAYBILL_BKK]
