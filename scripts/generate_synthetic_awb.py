#!/usr/bin/env python
"""
Generates fake air waybills with exact ground truth, from ONE real waybill PDF.

The real PDF is a full-page picture of the blank form with every value laid over it as a text
layer. So a new document is made by deleting that text layer and writing new text at the same
positions, same fonts and sizes; the form picture is untouched and the layout is the real one.
The ground truth is simply what was written, so it is exact.

    python scripts/generate_synthetic_awb.py --base REAL.pdf --out data/synthetic_awb --count 100
    python scripts/evaluate_batch.py --api http://localhost:8010 \\
        --images-dir data/synthetic_awb --ground-truth data/synthetic_awb/ground_truth.json

What the values are: random but plausible (Faker names and addresses, real IATA airport and airline
codes, AWB numbers whose check digit satisfies mod 7, and a total that really is weight x rate), and
every one passes the template's own format rules. Nothing from the source document is kept: all of its
text is removed, including the values that repeat outside the template's boxes (the AWB number at the
top-left and bottom, the totals row, the agent's signature), which are rewritten to match the new values.

What this is NOT: another airline's form, printer or paper. The layout, fonts and picture are the same
every time. It tests different CONTENT on the real layout, and scans/photos of it. Real documents
from other sources are still needed to know how the system does on them.

--inject-errors K makes K documents internally inconsistent on purpose (a wrong total, a misread
currency, a flight that does not match its carrier) and records which check should catch each in
injections.json, to prove the cross-field checks fire end to end.

Deterministic for a given --seed. Needs `pip install faker`.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import fitz  # noqa: E402
from faker import Faker  # noqa: E402

from scripts.build_synthetic_corpus import (  # noqa: E402
    add_noise, contrast, corner_presets, gamma, image_only_pdf, photograph, render_page, skew, speckle,
)
from scripts.drift import sample_drift, zone_by_height  # noqa: E402
from scripts.proto_select import ZONE  # noqa: E402  (which part of the page each field sits in)
from scripts.seed_templates import AIR_WAYBILL  # noqa: E402
from scripts.synth_formats import VARIED, rough_scan, vary, vary_line  # noqa: E402

TEMPLATE = AIR_WAYBILL
FIELD_BOX = {f.id: f.bbox for f in TEMPLATE.fields}
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

# (IATA prefix, carrier, 2-letter code). Only 176 is also in the template's maps_to table.
AIRLINES = [
    ("176", "EMIRATES", "EK"), ("157", "QATAR AIRWAYS", "QR"), ("618", "SINGAPORE AIRLINES", "SQ"),
    ("125", "BRITISH AIRWAYS", "BA"), ("074", "KLM", "KL"), ("235", "TURKISH AIRLINES", "TK"),
    ("098", "AIR INDIA", "AI"), ("020", "LUFTHANSA", "LH"),
]
AIRPORTS = [
    ("BANGALORE", "BLR"), ("MUMBAI", "BOM"), ("DELHI", "DEL"), ("CHENNAI", "MAA"), ("HYDERABAD", "HYD"),
    ("DUBAI", "DXB"), ("DOHA", "DOH"), ("SINGAPORE", "SIN"), ("LONDON", "LHR"), ("FRANKFURT", "FRA"),
    ("AMSTERDAM", "AMS"), ("ISTANBUL", "IST"), ("PARIS", "CDG"), ("NEW YORK", "JFK"), ("HONG KONG", "HKG"),
]
GOODS = [
    "PARTS OF AIR CONDITIONING M/C.", "AUTOMOTIVE SPARE PARTS", "ELECTRONIC COMPONENTS", "TEXTILE GARMENTS",
    "INDUSTRIAL PUMPS AND VALVES", "MEDICAL EQUIPMENT PARTS", "LED LIGHTING FIXTURES", "ALUMINIUM PROFILES",
    "COSMETIC PRODUCTS", "MACHINE TOOL ACCESSORIES",
]
CURRENCIES = ["INR", "USD", "AED", "EUR", "GBP", "SGD"]


def money(value: Decimal) -> str:
    """'11050' for a whole amount, '11050.50' otherwise: the way the real document writes them."""
    return str(int(value)) if value == value.to_integral_value() else f"{value:.2f}"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 &.,'/#:@-]", "", text.upper())).strip()


def make_values(rng: random.Random, fake: Faker, varied: bool = False) -> dict:
    prefix, carrier, code = rng.choice(AIRLINES)
    (o_city, o_code), (d_city, d_code) = rng.sample(AIRPORTS, 2)
    serial = rng.randrange(0, 10_000_000)
    awb = f"{prefix}-{serial:07d}{serial % 7}"  # IATA mod-7 check digit

    chargeable = Decimal(rng.randrange(10, 4000)) / (2 if rng.random() < 0.3 else 1)
    gross = chargeable if rng.random() < 0.7 else (chargeable * Decimal("0.8")).quantize(Decimal("0.5"))
    rate = Decimal(rng.randrange(20, 400)) if rng.random() < 0.6 else Decimal(rng.randrange(2000, 40000)) / 100
    rate_class = "Q" if rng.random() < 0.9 else "M"
    total = (chargeable * rate).quantize(Decimal("0.01")) if rate_class == "Q" else Decimal(rng.randrange(300, 900))
    agent_ch = Decimal(rng.randrange(100, 900))
    carrier_ch = Decimal(rng.randrange(200, 3000))
    charges = rng.choice(["PP", "CC"])
    day, month, year = rng.randrange(1, 29), rng.randrange(0, 12), rng.choice([2024, 2025, 2026])
    inv_year = year % 100

    def party_lines(n: int) -> list[str]:
        lines = [clean(fake.street_address()), clean(f"{fake.city()}, {fake.state()}"), clean(f"{fake.city()} {fake.postcode()}, {fake.current_country_code()}")]
        if n == 4:
            lines = [lines[0], lines[1], clean(f"P.O. BOX # {rng.randrange(1000, 99999)}, MOBILE: {fake.phone_number()}"), clean(f"EMAIL : {fake.email()}")]
        return [vary_line(ln, rng) for ln in lines] if varied else lines

    out = {
        "awb_number": awb, "carrier": carrier,
        "shipper_name": clean(fake.company()), "shipper_address": party_lines(3),
        "consignee_name": clean(fake.company()), "consignee_address": party_lines(4),
        "agent_name": clean(fake.company()),
        "agent_iata_code": f"{rng.randrange(10, 99)}/{rng.randrange(1, 99):02d}/{rng.randrange(1000, 9999):04d}",
        "airport_of_departure": f"{o_city} ({o_code})", "airport_of_destination": d_city, "destination_code": d_code,
        "first_carrier_code": code, "flight_date": f"{code}{rng.randrange(1, 9999):04d}/{day:02d}{MONTHS[month]}",
        "currency": rng.choice(CURRENCIES), "charges_code": charges,
        "declared_value_carriage": "NVD" if rng.random() < 0.85 else money(Decimal(rng.randrange(1000, 50000))),
        "declared_value_customs": "NVC" if rng.random() < 0.85 else money(Decimal(rng.randrange(1000, 50000))),
        "accounting_information": "FREIGHT PREPAID" if charges == "PP" else "FREIGHT COLLECT",
        "pieces_rcp": str(rng.randrange(1, 40)), "gross_weight": money(gross), "rate_class": rate_class,
        "chargeable_weight": money(chargeable), "rate_charge": money(rate), "total_charge": money(total),
        "nature_of_goods": rng.choice(GOODS), "hs_code": f"HS CODE: {rng.randrange(10**7, 10**8)}",
        "invoice_reference": f"INV: EX/{fake.state_abbr()}/{rng.randrange(1, 9999):04d}/{inv_year}-{inv_year + 1}",
        "prepaid_weight_charge": money(total), "total_other_charges_agent": f"{agent_ch:.2f}",
        "total_other_charges_carrier": f"{carrier_ch:.2f}", "total_prepaid": f"{total + agent_ch + carrier_ch:.2f}",
        "executed_date": f"{day:02d}-{MONTHS[month].title()}-{year}", "executed_place": o_city,
        # not template fields: values the real form repeats elsewhere
        "_prefix": prefix, "_serial8": awb.split("-")[1], "_origin_code": o_code, "_origin_city": o_city,
        "_date_dots": f"{day:02d}.{month + 1:02d}.{year}",
    }
    if varied:
        vary(out, rng, "M")
    return out


# --- injected inconsistencies ---------------------------------------------------------------------


def inject(values: dict, rng: random.Random) -> str:
    """Corrupt one value so a cross-field check should fail. Returns the id of the check that should catch it."""
    kind = rng.choice(["total", "currency", "flight"])
    if kind == "total" and values["rate_class"] == "Q":
        values["total_charge"] = money(Decimal(values["total_charge"]) + Decimal(rng.choice([100, 250, 600])))
        values["prepaid_weight_charge"] = values["total_charge"]
        return "total_equals_weight_times_rate"
    if kind == "currency":
        values["currency"] = rng.choice(["INF", "IMR", "UDS", "AEO"])  # one-letter misreads that a 3-letter rule accepts
        return "currency_is_iso_4217"
    code = values["first_carrier_code"]
    wrong = rng.choice([c for _, _, c in AIRLINES if c != code])
    values["flight_date"] = wrong + values["flight_date"][2:]
    return "flight_prefix_matches_first_carrier"


# --- writing the PDF ------------------------------------------------------------------------------


class Fonts:
    """
    Verdana, the font the real document uses (Helvetica if it is not installed), embedded by PyMuPDF.

    Two facts about that embed, both measured:
      * its word boxes have the real Verdana height (ascender 1.005, descender -0.209), which the
        template's tight boxes depend on; ReportLab's embed gave 0.765 and moved the boxes;
      * but its glyph-to-Unicode table maps the hyphen glyph to U+00AD and the space glyph to U+00A0, which
        would break the AWB format check. fix_unicode() rewrites that table for the glyphs actually used.
    """

    PATHS = {"regular": "C:/Windows/Fonts/verdana.ttf", "bold": "C:/Windows/Fonts/verdanab.ttf"}

    def __init__(self) -> None:
        self.files = {k: v for k, v in self.PATHS.items() if Path(v).exists()}
        self.metric = {k: fitz.Font(fontfile=v) for k, v in self.files.items()}
        self.builtin = {"regular": fitz.Font("helv"), "bold": fitz.Font("hebo")}

    def register(self, page: "fitz.Page") -> None:
        for kind, path in self.files.items():
            page.insert_font(fontname=f"V{kind}", fontfile=path)

    def name(self, bold: bool) -> str:
        kind = "bold" if bold else "regular"
        return f"V{kind}" if kind in self.files else ("hebo" if bold else "helv")

    def width(self, text: str, size: float, bold: bool) -> float:
        kind = "bold" if bold else "regular"
        return (self.metric.get(kind) or self.builtin[kind]).text_length(text, fontsize=size)

    def fix_unicode(self, doc: "fitz.Document", page: "fitz.Page", used: dict[str, set[str]]) -> None:
        """Replace each embedded Verdana's ToUnicode CMap with one built from the characters actually written."""
        for info in page.get_fonts(full=True):
            xref, refname = info[0], info[4]
            kind = refname[1:] if refname in ("Vregular", "Vbold") else None
            if kind is None or kind not in self.metric:
                continue
            match = re.search(r"/ToUnicode (\d+) 0 R", doc.xref_object(xref, compressed=False))
            if not match:
                raise RuntimeError(f"embedded font {refname} has no ToUnicode map to repair")
            pairs = sorted({(self.metric[kind].has_glyph(ord(ch)), ord(ch)) for ch in used[kind]})
            if any(gid == 0 for gid, _ in pairs):
                raise ValueError(f"Verdana has no glyph for some of {sorted(used[kind])}")
            lines = ["/CIDInit /ProcSet findresource begin", "12 dict begin", "begincmap",
                     "/CIDSystemInfo <</Registry(Adobe)/Ordering(UCS)/Supplement 0>> def",
                     "/CMapName /Adobe-Identity-UCS def", "/CMapType 2 def",
                     "1 begincodespacerange", "<0000> <FFFF>", "endcodespacerange"]
            for i in range(0, len(pairs), 100):
                chunk = pairs[i : i + 100]
                lines.append(f"{len(chunk)} beginbfchar")
                lines += [f"<{gid:04x}> <{code:04x}>" for gid, code in chunk]
                lines.append("endbfchar")
            lines += ["endcmap", "CMapName currentdict /CMap defineresource pop", "end", "end"]
            doc.update_stream(int(match.group(1)), "\n".join(lines).encode("latin1"))


class Base14:
    """A built-in typeface with Fonts' interface: the typed layer of a document another printer produced."""

    files: dict = {}

    def __init__(self, regular: str, bold: str) -> None:
        self.names = {False: regular, True: bold}
        self.fonts = {False: fitz.Font(regular), True: fitz.Font(bold)}

    def register(self, page: "fitz.Page") -> None:
        pass

    def name(self, bold: bool) -> str:
        return self.names[bold]

    def width(self, text: str, size: float, bold: bool) -> float:
        return self.fonts[bold].text_length(text, fontsize=size)


def collect_spans(base: bytes, fonts: Fonts) -> tuple[list[dict], tuple[float, float]]:
    """Every text span of the source page. A span holding several column-aligned parts ('K  Q': a unit
    marker, two spaces, then the rate class) is split into one pseudo-span per part, so a value is replaced
    on its own and the text beside it stays where it was."""
    doc = fitz.open(stream=base, filetype="pdf")
    page = doc[0]
    spans = []
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            for s in ln["spans"]:
                if not s["text"].strip():
                    continue
                parts = list(re.finditer(r"\S+(?: \S+)*", s["text"]))
                if len(parts) <= 1:
                    spans.append(s)
                    continue
                bold = "Bold" in s["font"]
                for m in parts:
                    x = s["origin"][0] + fonts.width(s["text"][: m.start()], s["size"], bold)
                    w = fonts.width(m.group(), s["size"], bold)
                    spans.append({**s, "text": m.group(), "origin": (x, s["origin"][1]), "bbox": (x, s["bbox"][1], x + w, s["bbox"][3])})
    size = (page.rect.width, page.rect.height)
    doc.close()
    return spans, size


def _overlap(inner: tuple, outer: tuple) -> float:
    ix = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    area = (inner[2] - inner[0]) * (inner[3] - inner[1])
    return (ix * iy) / area if area else 0.0


def plan_placements(spans: list[dict], page_size: tuple, values: dict) -> tuple[list[dict], dict[str, str]]:
    """Decide the new text for every original span. Returns (placements, ground-truth field values)."""
    w, h = page_size
    by_field: dict[str, list[dict]] = {}
    free: list[dict] = []
    for s in spans:
        frac = (s["bbox"][0] / w, s["bbox"][1] / h, s["bbox"][2] / w, s["bbox"][3] / h)
        owner = next((f.id for f in TEMPLATE.fields if _overlap(frac, f.bbox) >= 0.5), None)
        (by_field.setdefault(owner, []).append(s) if owner else free.append(s))

    def place(s: dict, text: str, x_limit: float, zone: str | None = None, fid: str | None = None) -> dict:
        return {"fid": fid, "origin": s["origin"], "size": s["size"], "bold": "Bold" in s["font"], "text": text,
                "x_limit": x_limit, "lead": len(s["text"]) - len(s["text"].lstrip()),
                "zone": zone or zone_by_height(s["origin"][1] / h)}

    placements, truth = [], {}
    for field in TEMPLATE.fields:
        slots = sorted(by_field.get(field.id, []), key=lambda s: (round(s["origin"][1]), s["origin"][0]))
        lines = values[field.id] if isinstance(values[field.id], list) else [values[field.id]]
        lines = lines[: len(slots)] if len(slots) < len(lines) else lines
        for slot, text in zip(slots, lines):
            if text:
                placements.append(place(slot, text, field.bbox[2] * w, ZONE[field.id], field.id))
        truth[field.id] = " ".join(lines)

    # values the real form repeats outside any template box, matched by their ORIGINAL text
    echo = {"176": values["_prefix"], "06067736": values["_serial8"], "BLR": values["_origin_code"],
            "BANGALORE": values["_origin_city"], "11050": values["total_charge"], "65": values["gross_weight"],
            "1": values["pieces_rcp"], "176-06067736": values["awb_number"], "TEAMGLOBAL LOGISTICS PVT LTD": values["agent_name"],
            "DATE: 26.02.2025": f"DATE: {values['_date_dots']}", "CUSTOMS FORMALITIES COMPLETED AT BLR": f"CUSTOMS FORMALITIES COMPLETED AT {values['_origin_code']}"}
    for s in free:
        key = s["text"].strip()
        if key in echo:
            placements.append(place(s, echo[key], s["bbox"][2] + 60))
        else:  # generic printed text (handling, dimensions, other charges): kept as it was
            placements.append(place(s, key, s["bbox"][2] + 4))
    return placements, truth


def build_pdf(base: bytes, spans: list[dict], page_size: tuple, values: dict, fonts: Fonts, drift: dict | None = None, overflow: bool = False) -> tuple[bytes, dict[str, str]]:
    placements, truth = plan_placements(spans, page_size, values)
    fitted = []
    for p in placements:
        x, y = p["origin"]
        size = p["size"]
        x += fonts.width(" " * p["lead"], size, p["bold"])  # the real document positions some values with leading spaces
        while fonts.width(p["text"], size, p["bold"]) > p["x_limit"] - x and size > 5.5:
            size -= 0.25
        if fonts.width(p["text"], size, p["bold"]) > p["x_limit"] - x and not (overflow and p.get("fid") in VARIED):
            raise ValueError(f"text does not fit its box even at {size}pt: {p['text']!r}")  # a value printed past its cell: real forms do that (--varied)
        fitted.append((x, y, size, p))

    doc = fitz.open(stream=base, filetype="pdf")
    page = doc[0]
    for s in spans:
        page.add_redact_annot(fitz.Rect(s["bbox"]))
    # remove the old text only: the form picture and ruled lines stay exactly as they are
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)

    fonts.register(page)
    used: dict[str, set[str]] = {"regular": set(), "bold": set()}
    boxes: dict[str, list[float]] = {}
    for x, y, size, p in fitted:
        dx, dy = (drift or {}).get(p["zone"], (0.0, 0.0))  # the typed layer sits off the printed form by this much
        page.insert_text((x + dx * page_size[0], y + dy * page_size[1]), p["text"], fontname=fonts.name(p["bold"]), fontsize=size, color=(0, 0, 0))
        if p.get("fid"):  # where this line ended up, as page fractions
            W_, H_ = page_size
            b = [(x + dx * W_) / W_, (y + dy * H_ - 0.78 * size) / H_, (x + dx * W_ + fonts.width(p["text"], size, p["bold"])) / W_, (y + dy * H_ + 0.22 * size) / H_]
            o = boxes.get(p["fid"])
            boxes[p["fid"]] = b if not o else [min(o[0], b[0]), min(o[1], b[1]), max(o[2], b[2]), max(o[3], b[3])]
        used["bold" if p["bold"] else "regular"].update(p["text"])
    build_pdf.last_boxes = {k: [round(c, 4) for c in v] for k, v in boxes.items()}
    if fonts.files:
        fonts.fix_unicode(doc, page, used)
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out, truth


# --- scan and photo variants ----------------------------------------------------------------------


def scan_variant(pdf: bytes, rng: random.Random, seed: int, hard: bool = False, varied: bool = False) -> bytes:
    page = render_page(fitz.open(stream=pdf, filetype="pdf"), 200)
    if varied:
        page = rough_scan(page, rng, hard)
    if hard:  # a worse scanner: more skew, noise on every page, low contrast and speckle often, heavier compression
        page = skew(page, rng.uniform(-3.0, 3.0))
        page = add_noise(page, rng.uniform(8, 18), seed)
        if rng.random() < 0.6:
            page = contrast(page, rng.uniform(0.5, 0.85), rng.uniform(20, 90))
        if rng.random() < 0.5:
            page = speckle(gamma(page, rng.uniform(1.2, 1.9)), 0.004, seed)
        return image_only_pdf(page, jpeg_quality=rng.choice([45, 55, 70]))
    page = skew(page, rng.uniform(-2.0, 2.0))
    if rng.random() < 0.6:
        page = add_noise(page, rng.uniform(4, 12), seed)
    if rng.random() < 0.3:
        page = contrast(page, rng.uniform(0.55, 0.9), rng.uniform(20, 90))
    if rng.random() < 0.2:
        page = speckle(gamma(page, rng.uniform(1.2, 1.9)), 0.003, seed)
    return image_only_pdf(page, jpeg_quality=rng.choice([55, 70, 88]))


def photo_variant(pdf: bytes, rng: random.Random, seed: int) -> bytes:
    page = render_page(fitz.open(stream=pdf, filetype="pdf"), 200)
    presets = corner_presets(page.shape[1])
    return photograph(
        page, presets[rng.choice(["zero", "mild", "rotated", "keystone"])],
        brightness=rng.uniform(0.7, 1.0), blur=rng.choice([0, 3, 5]), shadow=rng.uniform(0, 0.4),
        noise=rng.uniform(0, 9), jpeg_quality=rng.choice([60, 75, 88]), downscale=rng.choice([0.6, 0.8, 1.0]),
        margin=rng.uniform(0.1, 0.4), seed=seed,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, type=Path, help="the real waybill PDF (a picture of the form + a text layer)")
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "synthetic_awb")
    ap.add_argument("--count", type=int, default=100, help="digital PDFs to make")
    ap.add_argument("--scans", type=int, default=30, help="how many of them also get a scanned-PDF copy")
    ap.add_argument("--photos", type=int, default=20, help="how many of them also get a camera-photo copy")
    ap.add_argument("--inject-errors", type=int, default=0, help="make this many documents internally inconsistent")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--hard", action="store_true", help="worse print misregistration and a worse scanner than the default")
    ap.add_argument("--drift", action="store_true", help="shift the typed layer against the printed form, zone by zone (scripts/drift.py)")
    ap.add_argument("--varied", action="store_true", help="the formats, fonts and scan faults of scripts/synth_formats.py")
    args = ap.parse_args()

    base = args.base.read_bytes()
    fonts = Fonts()
    spans, page_size = collect_spans(base, fonts)
    fake = Faker(["en_IN", "en_GB", "en_US"])
    fake.seed_instance(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)

    truth_file: dict[str, dict] = {}
    injections: dict[str, str] = {}
    inject_ids = set(random.Random(args.seed).sample(range(args.count), min(args.inject_errors, args.count)))
    for i in range(args.count):
        name = f"awb_{i:04d}"
        for attempt in range(40):  # a drawn name can be too wide for its box: draw again, as a clerk would shorten it
            rng = random.Random(args.seed * 100_003 + i + attempt * 7_919)
            values = make_values(rng, fake, args.varied)
            injected = inject(values, rng) if i in inject_ids else None
            try:
                drift = (sample_drift(rng, bad_print=0.6, scale=1.8) if args.hard else sample_drift(rng)) if args.drift else None
                doc_fonts = rng.choice([fonts, fonts, Base14("helv", "hebo"), Base14("cour", "cobo"), Base14("tiro", "tibo")]) if args.varied else fonts
                pdf, fields = build_pdf(base, spans, page_size, values, doc_fonts, drift, overflow=args.varied)
                break
            except ValueError:
                continue
        else:
            raise SystemExit(f"could not fit values for {name} in 40 draws")
        if injected:
            injections[f"{name}.pdf"] = injected
        entry = {"template_id": "air_waybill", "fields": fields, "boxes": build_pdf.last_boxes, **({"drift": {z: [round(a, 4), round(b, 4)] for z, (a, b) in drift.items()}} if drift else {})}
        (args.out / f"{name}.pdf").write_bytes(pdf)
        truth_file[f"{name}.pdf"] = entry
        if i < args.scans:
            (args.out / f"{name}__scan.pdf").write_bytes(scan_variant(pdf, rng, args.seed + i, args.hard, args.varied))
            truth_file[f"{name}__scan.pdf"] = entry
        if i < args.photos:
            (args.out / f"{name}__photo.jpg").write_bytes(photo_variant(pdf, rng, args.seed + i))
            truth_file[f"{name}__photo.jpg"] = entry

    (args.out / "ground_truth.json").write_text(json.dumps(truth_file, indent=2), encoding="utf-8")
    if injections:
        (args.out / "injections.json").write_text(json.dumps(injections, indent=2), encoding="utf-8")
    print(f"wrote {len(truth_file)} files to {args.out} ({args.count} digital, {args.scans} scans, {args.photos} photos, {len(injections)} injected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
