"""
Whole-page reader: reads an air waybill page with NO template.

A pretrained vision model (an engine with role "page" in the engines file) is shown the page image and asked, once, for the 33 air_waybill
fields as JSON. The prompt is written ONCE from the field definitions below (what each field means on an IATA air waybill and its format);
it carries no value from any document. Output per field: {"value", "source_text"} ("" when the field is absent or unreadable).

Failure never crashes a batch: invalid JSON is retried once, and after that (or after a timeout / connection error / bad status) every field
is "" with reason `reader_failed`. The server's JSON-schema mode (`response_format`) is used when the engine's `extra_body` does not turn it off.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from PIL import Image

from app.modules.vlm_reader.engines import EngineSpec

log = logging.getLogger(__name__)

PAGE_MAX_LONG_EDGE_PX = 2000  # the page is scaled down to this long edge before it is sent (never up); recorded in PageRead.image_size
REQUEST_TIMEOUT_S = 120.0

# (field id, label printed on the form, what it holds and its format). Formats are the template's own validation shapes, not examples.
FIELD_DEFS: list[tuple[str, str, str]] = [
    ("awb_number", "Air Waybill number", "the 11-digit number printed at the top of the form (and again at the bottom): a 3-digit airline prefix, a hyphen, an 8-digit serial; written NNN-NNNNNNNN"),
    ("carrier", "Carrier", "the name of the airline that issued the waybill (letters and spaces), as printed in the header"),
    ("shipper_name", "Shipper's Name", "the name line of the 'Shipper's Name and Address' box, the first line only"),
    ("shipper_address", "Shipper's Address", "the rest of the 'Shipper's Name and Address' box after the name: street, city, country, phone; lines joined by one space"),
    ("consignee_name", "Consignee's Name", "the name line of the 'Consignee's Name and Address' box, the first line only"),
    ("consignee_address", "Consignee's Address", "the rest of the 'Consignee's Name and Address' box after the name; lines joined by one space"),
    ("agent_name", "Issuing Carrier's Agent", "the 'Issuing Carrier's Agent Name and City' box: the agent's name and city"),
    ("agent_iata_code", "Agent's IATA Code", "the 'Agent's IATA Code' cell: digits, possibly with a slash or hyphen (6 to 20 characters)"),
    ("airport_of_departure", "Airport of Departure", "the 'Airport of Departure (Addr. of first Carrier) and requested Routing' cell: the departure airport / city as written"),
    ("airport_of_destination", "Airport of Destination", "the 'Airport of Destination' cell: the destination airport / city as written (letters, spaces, dots)"),
    ("destination_code", "Destination Code", "the 3-letter IATA airport code of the destination, in the routing row 'To' column"),
    ("first_carrier_code", "By First Carrier", "the 2-character airline code in the routing row's 'By first carrier' column"),
    ("flight_date", "Flight / Date", "the 'Flight/Date' cell: the flight designator (2 letters + 4 digits) a slash and the day and month, written CC9999/DDMMM with the month as 3 capital letters"),
    ("currency", "Currency", "the 'Currency' cell: a 3-letter ISO 4217 currency code"),
    ("charges_code", "CHGS Code", "the 'CHGS Code' cell: a 2-letter charges code such as the prepaid / collect pair"),
    ("declared_value_carriage", "Declared Value for Carriage", "the 'Declared Value for Carriage' cell: an amount, or the printed 'no value declared' abbreviation"),
    ("declared_value_customs", "Declared Value for Customs", "the 'Declared Value for Customs' cell: an amount, or the printed 'no customs value' abbreviation"),
    ("accounting_information", "Accounting Information", "the free-text 'Accounting Information' box, lines joined by one space"),
    ("pieces_rcp", "No. of Pieces RCP", "the number of pieces in the first goods row: a whole number of 1 to 4 digits"),
    ("gross_weight", "Gross Weight", "the gross weight in the first goods row: digits with a decimal point"),
    ("rate_class", "Rate Class", "the single capital letter in the 'Rate Class' column of the first goods row"),
    ("chargeable_weight", "Chargeable Weight", "the chargeable weight in the first goods row: digits with a decimal point"),
    ("rate_charge", "Rate / Charge", "the rate per kilo (or minimum charge) in the first goods row: digits with a decimal point"),
    ("total_charge", "Total", "the 'Total' of the first goods row: digits with a decimal point and optional thousands commas"),
    ("nature_of_goods", "Nature and Quantity of Goods", "the goods description in the 'Nature and Quantity of Goods (incl. Dimensions or Volume)' column, without a tariff code or invoice reference that is printed inside it; lines joined by one space"),
    ("hs_code", "HS Code", "a harmonised-system tariff code written inside the goods description, usually after the words HS CODE; empty when none is written"),
    ("invoice_reference", "Invoice Reference", "an invoice number written inside the goods description, usually after INV; empty when none is written"),
    ("prepaid_weight_charge", "Weight Charge Prepaid", "the 'Prepaid' column of the 'Weight Charge' line in the charges summary: digits with a decimal point"),
    ("total_other_charges_agent", "Total Other Charges Due Agent", "the amount on the 'Total Other Charges Due Agent' line of the charges summary: digits with a decimal point"),
    ("total_other_charges_carrier", "Total Other Charges Due Carrier", "the amount on the 'Total Other Charges Due Carrier' line of the charges summary: digits with a decimal point"),
    ("total_prepaid", "Total Prepaid", "the amount in the 'Total Prepaid' cell of the charges summary: digits with a decimal point"),
    ("executed_date", "Executed on (date)", "the date in the 'Executed on (date)' cell at the bottom, as written"),
    ("executed_place", "at (place)", "the place in the 'at (place)' cell at the bottom, as written"),
]
FIELD_IDS = [fid for fid, _l, _d in FIELD_DEFS]

# Variant B (wording only, no values): fields the reader confused with a neighbour, defined more narrowly.
SHARPER_DEFS = {
    "destination_code": "the 3-letter IATA airport code in the FIRST 'To' cell of the routing row (first leg of the requested routing); it can differ from the airport of final destination, so do not copy the final destination's code",
    "flight_date": "the first flight in the 'Flight/Date' cell only: the flight designator (2 letters + 3 or 4 digits), a slash and the day (and month when printed); when several flights are listed, give only the first",
}
TOP_FIELDS = FIELD_IDS[:FIELD_IDS.index("pieces_rcp")]  # header, parties, routing, accounting: the upper part of an IATA form
BOTTOM_FIELDS = FIELD_IDS[FIELD_IDS.index("pieces_rcp"):]  # goods rows, charges, execution: the lower part

SYSTEM_RULES = (
    "You read one IATA air waybill page and return what is printed or typed on it, as JSON only. "
    "Copy each value exactly as it appears on the page: do not correct, translate, reformat or complete it, and do not use outside knowledge. "
    "A field you cannot find, or cannot read with confidence, gets an empty string for both its value and its source_text. "
    "Never guess a value, and never fill a field with the value of another field. "
    "For every field give 'value' (the text of that field) and 'source_text' (the exact words on the page the value was taken from; "
    "for a long value, the first line of it)."
)


def build_prompt(layer_text: Optional[str] = None, only: Optional[list[str]] = None, sharper: bool = False, part: Optional[str] = None) -> str:
    """The one question. `only` restricts it to the named fields (the second pass for missing ones); `layer_text` adds the page's embedded text."""
    ids = only or FIELD_IDS
    lines = [f'- "{fid}" ({label}): {SHARPER_DEFS[fid] if sharper and fid in SHARPER_DEFS else desc}' for fid, label, desc in FIELD_DEFS if fid in ids]
    text = (f"This image is the {part} part of the page; only the fields listed below can be in it.\n\n" if part else "") + SYSTEM_RULES + "\n\nFields:\n" + "\n".join(lines) + (
        '\n\nAnswer with one JSON object whose keys are exactly the field ids above, each mapping to {"value": "...", "source_text": "..."}.'
    )
    if layer_text:
        text += ("\n\nThe PDF's embedded text for this page is given below as extra help; its order and position can be off, so trust the image "
                 "where they disagree.\n<embedded_text>\n" + layer_text[:6000] + "\n</embedded_text>")
    return text


def build_skeleton_prompt(only: Optional[list[str]] = None) -> str:
    """The extraction wording OCR-specialised parsers (GLM-OCR) are trained on: 'output the information in this JSON format', with the
    field list as a skeleton. Short labels, no explanations; values only (no source_text)."""
    ids = only or FIELD_IDS
    skeleton = "{\n" + ",\n".join(f'  "{fid}": ""  // {label}' for fid, label, _d in FIELD_DEFS if fid in ids) + "\n}"
    return ("Please output the information in the image in the following JSON format, copying each value exactly as printed on this IATA air "
            'waybill and leaving "" for a field that is not on the page:\n' + skeleton)


def build_schema(only: Optional[list[str]] = None) -> dict:
    ids = only or FIELD_IDS
    cell = {"type": "object", "properties": {"value": {"type": "string"}, "source_text": {"type": "string"}}, "required": ["value", "source_text"]}
    return {"type": "object", "properties": {fid: cell for fid in ids}, "required": list(ids)}


@dataclass
class ReadField:
    value: str = ""
    source_text: str = ""


@dataclass
class PageRead:
    fields: dict[str, ReadField] = field(default_factory=lambda: {fid: ReadField() for fid in FIELD_IDS})
    ok: bool = False
    reason: Optional[str] = None  # None when ok, else e.g. "reader_failed"
    engine: str = ""
    seconds: float = 0.0
    image_size: tuple[int, int] = (0, 0)  # what was sent
    attempts: int = 0


def encode_image(png: bytes, max_long_edge: int = PAGE_MAX_LONG_EDGE_PX) -> tuple[str, tuple[int, int]]:
    """The page as a base64 PNG, scaled down (never up) so its long edge is at most `max_long_edge`."""
    img = Image.open(io.BytesIO(png)).convert("RGB")
    long_edge = max(img.size)
    if long_edge > max_long_edge:
        scale = max_long_edge / long_edge
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode(), img.size


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def parse_reply(text: str, ids: list[str]) -> Optional[dict[str, ReadField]]:
    """The reply's JSON as {field id: ReadField}; None when it is not a JSON object. Missing or non-string entries become ''."""
    body = _FENCE.sub("", text.strip())
    try:
        data = json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(body[start:end + 1])
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    out: dict[str, ReadField] = {}
    for fid in ids:
        cell = data.get(fid)
        if isinstance(cell, dict):
            value, source = cell.get("value"), cell.get("source_text")
        else:
            value, source = cell, ""
        out[fid] = ReadField(
            value=" ".join(value.split()) if isinstance(value, str) else ("" if value is None else str(value)),
            source_text=" ".join(source.split()) if isinstance(source, str) else "",
        )
    return out


_THOUSANDS_DOTS = re.compile(r"^\d{1,3}(?:\.\d{3})+\.\d{2}$")
_AWB_COMPACT = re.compile(r"^(\d{3})-?(\d{8})$")
_LEADING_UNIT = re.compile(r"^[A-Za-z]{1,3}\s+(?=[\d.,]+$)")


def normalise_value(fid: str, value: str) -> str:
    """Deterministic repairs by data type (never by document): spaces inside the AWB number, a unit word before an amount, '.' used as a
    thousands separator. Anything that does not fit its pattern is returned unchanged."""
    from app.utils.compare import NUMERIC_FIELDS

    if fid == "awb_number":
        m = _AWB_COMPACT.match("".join(value.split()))
        return f"{m.group(1)}-{m.group(2)}" if m else value
    if fid in NUMERIC_FIELDS:
        v = _LEADING_UNIT.sub("", value.strip())
        v = "".join(v.split()) if re.fullmatch(r"[\d.,\s]+", v) else v
        if _THOUSANDS_DOTS.match(v):
            head, tail = v.rsplit(".", 1)
            v = head.replace(".", ",") + "." + tail
        return v
    return value


def _call(spec: EngineSpec, image_b64: str, prompt: str, schema: Optional[dict], transport=None) -> str:
    headers = {}
    token = os.environ.get(spec.token_env, "") if spec.token_env else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "model": spec.model, "temperature": 0, "max_tokens": spec.max_tokens or 4096,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}},
            {"type": "text", "text": prompt}]}],
        **spec.extra_body,
    }
    if schema is not None and "response_format" not in body:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "awb_fields", "schema": schema, "strict": True}}
    timeout = httpx.Timeout(spec.timeout_s or REQUEST_TIMEOUT_S, connect=spec.connect_timeout_s or 10.0)
    with httpx.Client(timeout=timeout, transport=transport) as client:
        r = client.post(spec.url.rstrip("/") + "/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"] or ""


def read_page(png: bytes, spec: EngineSpec, *, layer_text: Optional[str] = None, only: Optional[list[str]] = None,
              max_long_edge: int = PAGE_MAX_LONG_EDGE_PX, transport=None, normalise: bool = False, sharper: bool = False,
              halves: bool = False, part: Optional[str] = None, skeleton: bool = False) -> PageRead:
    """Read one page image with one engine. Never raises. `normalise`, `sharper`, `halves` are the measured variants (see docs/reader-results.md)."""
    if halves and part is None:
        return _read_with_halves(png, spec, layer_text=layer_text, only=only, max_long_edge=max_long_edge, transport=transport, normalise=normalise, sharper=sharper)
    ids = list(only or FIELD_IDS)
    result = PageRead(engine=spec.name)
    t0 = time.time()
    try:
        image_b64, result.image_size = encode_image(png, max_long_edge)
    except Exception as exc:  # an unreadable image is this page's failure, not the batch's
        log.warning("page reader %s: cannot decode the page image: %s", spec.name, exc)
        result.reason = "reader_failed"
        return result
    prompt, schema = (build_skeleton_prompt(only), None) if skeleton else (build_prompt(layer_text, only, sharper=sharper, part=part), build_schema(only))
    for attempt in (1, 2):
        result.attempts = attempt
        try:
            reply = _call(spec, image_b64, prompt, schema, transport)
        except Exception as exc:  # timeout, connection error, bad status, odd body
            log.warning("page reader %s attempt %d failed: %s", spec.name, attempt, exc)
            continue
        parsed = parse_reply(reply, ids)
        if parsed is not None:
            for fid, rf in parsed.items():
                result.fields[fid] = rf
            result.ok = True
            break
        log.warning("page reader %s attempt %d: the reply was not a JSON object", spec.name, attempt)
    if not result.ok:
        result.reason = "reader_failed"
    elif normalise:
        for fid, rf in result.fields.items():
            rf.value = normalise_value(fid, rf.value)
    result.seconds = round(time.time() - t0, 2)
    return result


def _crop_png(png: bytes, top: float, bottom: float) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    out = io.BytesIO()
    img.crop((0, round(img.height * top), img.width, round(img.height * bottom))).save(out, "PNG")
    return out.getvalue()


def _read_with_halves(png: bytes, spec: EngineSpec, *, layer_text, only, max_long_edge, transport, normalise, sharper) -> PageRead:
    """Variant C (small text): the whole page, then the upper and lower part each at the page's own resolution (10% overlap), merged by field:
    a field of the upper (lower) part takes the part's value when it has one, else the whole-page value."""
    full = read_page(png, spec, layer_text=layer_text, only=only, max_long_edge=max_long_edge, transport=transport, normalise=normalise, sharper=sharper)
    parts = (("upper", _crop_png(png, 0.0, 0.55), [f for f in TOP_FIELDS if not only or f in only]),
             ("lower", _crop_png(png, 0.45, 1.0), [f for f in BOTTOM_FIELDS if not only or f in only]))
    for name, crop, ids in parts:
        if not ids:
            continue
        r = read_page(crop, spec, only=ids, max_long_edge=max(max_long_edge, 2600), transport=transport, normalise=normalise, sharper=sharper, part=name)
        full.seconds += r.seconds
        if r.ok:
            full.ok = True
            full.reason = None
            for fid in ids:
                if r.fields[fid].value:
                    full.fields[fid] = r.fields[fid]
    return full
