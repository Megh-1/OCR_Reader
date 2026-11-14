"""
Fake Bangkok-form air waybills with the print misregistration the real scans have, with exact ground truth.

The base is a real text-native waybill of this form (#8 in data/real_awb): the printed form is vector art and the typed
values are a separate text layer, so a fake is made by redacting the typed values and writing Faker values at the same
origins - and then SHIFTING the typed layer against the form, a different small amount in each zone of the page, the way
a pre-printed form fed slightly off in a printer is typed on. Some documents drift far enough that a value leaves its
cell. Each document is also written as a scan (rendered, skewed, noised, JPEG-compressed).

The ground truth follows the labelling conventions of data/real_awb/labels: the AWB number without spaces, the chargeable
weight without the printed "AS ", an HS code as digits, the goods description as the text before the first marker.

    python scripts/synth_bkk.py --count 50 --seed 11 [--out data/synthetic_bkk]

It cannot make anything the base does not show: another airline's different printer, handwriting, a stamp. Faker's names
and addresses are generic, so the values' distribution is simpler than real shippers'.
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

from scripts import proto_select as P  # noqa: E402  (the zone each field belongs to)
from scripts.drift import sample_drift  # noqa: E402
from scripts.build_synthetic_corpus import add_noise, contrast, gamma, image_only_pdf, render_page, skew, speckle  # noqa: E402
from scripts.generate_synthetic_awb import clean  # noqa: E402
from scripts.synth_formats import VARIED, rough_scan, vary, vary_line  # noqa: E402
from scripts.waybill_forms import AIR_WAYBILL_BKK as TEMPLATE  # noqa: E402

BASE = next((PROJECT_ROOT / "data" / "real_awb").glob("*/17602195281*.pdf"))
TYPED = "hebo"  # Helvetica-Bold: the base's typed text is Arial Bold at the same metrics

# (prefix, carrier as the form prints it, designator). Airline and airport names are public vocabulary. GOODS below holds descriptions invented for
# the generator: none is a value read off a gold real waybill (scripts/check_gold_isolation.py searches for them).
AIRLINES = [("176", "EMIRATES", "EK"), ("205", "ALL NIPPON AIRWAYS", "NH"), ("217", "THAI", "TG"), ("057", "AIR FRANCE", "AF"),
            ("514", "AIR ARABIA", "G9"), ("157", "QATAR AIRWAYS", "QR"), ("724", "SWISS WORLDCARGO", "LX"), ("312", "INDIGO", "6E")]
DESTINATIONS = [("BEIRUT,LEBANON", "BEY"), ("DOHA,QATAR", "DOH"), ("CHICAGO, USA", "ORD"), ("LOS ANGELES, USA", "LAX"), ("ZURICH,SWITZERLAND.", "ZRH"),
                ("FRANKFURT, GERMANY", "FRA"), ("BRUSSELS,BELGIUM", "BRU"), ("KUNMING, CHINA", "KMG"), ("SHARJAH,UAE", "SHJ"),
                ("CHENNAI(MADRAS)", "MAA"), ("LONDON,UK", "LHR"), ("DUBAI,UAE", "DXB")]
GOODS = [["GARMENTS,HANGERS,", "ACCESSORIES, AND SCARF"], ["CONSOL SHIPMENT AS PER", "ATTACHED MANIFEST"], ['"PERSONAL EFFECTS"'], ["ASHES"],
         ["FROZEN SEAFOOD DUMPLINGS", "PACKED IN CARTONS"], ["FRESH CUT FLOWERS", "ROSES AND ORCHIDS"], ["ELECTRONIC COMPONENTS"],
         ["AUTOMOTIVE SPARE PARTS"], ["WATCH PARTS", "STAINLESS STEEL WATCH CASES"], ["HANDICRAFT WOODEN ITEMS"], ["MEDICAL EQUIPMENT PARTS"],
         ["CLOTHING AND ACCESSORIES"], ["LED LIGHTING FIXTURES"], ["SOLAR INVERTER 2KW SPARES"]]
AGENTS = ["SKX", "EWL", "RKI", "EXL", "TFC", "PAC", "AGT"]


def money(x: Decimal) -> str:
    return f"{x:,.2f}"


def make_values(rng: random.Random, fake: Faker, varied: bool = False) -> tuple[dict, dict]:
    """(printed lines per field, ground truth per field)"""
    prefix, carrier, code = rng.choice(AIRLINES)
    dest_city, dest_code = rng.choice(DESTINATIONS)
    serial7 = rng.randrange(0, 10_000_000)
    serial8 = f"{serial7:07d}{serial7 % 7}"  # IATA mod-7 check digit
    gross = Decimal(rng.randrange(2, 2500)) + Decimal(rng.choice([0, 0.5, 0.25, 0.75]))
    chargeable = max(gross, (gross * Decimal(rng.choice(["1", "1.1", "1.4", "0.95"]))).quantize(Decimal("0.5")))
    q = rng.random() < 0.88
    rate = Decimal(rng.randrange(20, 400)) + Decimal(rng.choice([0, 0.5]))
    total = (chargeable * rate).quantize(Decimal("0.01")) if q else Decimal(rng.randrange(300, 1800))
    agent_ch, carrier_ch = Decimal(rng.randrange(0, 700)), Decimal(rng.randrange(0, 4000))
    if agent_ch == 0:
        agent_ch = Decimal(40)
    day, month, year = rng.randrange(1, 29), rng.randrange(1, 13), rng.choice([2024, 2025])
    flight_day = rng.randrange(28, 32)

    def party(name_len=36, lines=3, tel=False):
        name = clean(fake.company())[:name_len].rstrip()
        out = [clean(fake.street_address())[:34], clean(f"{fake.city()} {fake.postcode()}")[:34]]
        if lines >= 3:
            out.append(clean(f"{fake.current_country()}")[:34])
        if tel:
            out.append(f"TEL {rng.randrange(10**7, 10**8)}")
        return name, [vary_line(l, rng) if varied else l for l in out if l]

    s_name, s_addr = party(lines=rng.choice([2, 3]))
    c_name, c_addr = party(lines=3, tel=rng.random() < 0.7)
    desc = rng.choice(GOODS)
    hs = f"{rng.randrange(10**5, 10**8)}" if rng.random() < 0.65 else ""
    if varied and hs and rng.random() < 0.35:
        hs = f"{rng.randrange(1000, 9999)}.{rng.randrange(100):02d}"
    inv = f"{rng.choice(['EX', 'TF', 'SAM', 'AH'])}/{rng.randrange(1, 9999):04d}/{year % 100}-{year % 100 + 1}" if rng.random() < 0.4 else ""
    goods_lines = list(desc)
    if hs:
        goods_lines.append(rng.choice([f"H.S.CODE : {hs}", f"HS CODE:{hs}", f"HS CODE {hs}"]))
    if inv:
        goods_lines.append(f"INV NO : {inv}")
    goods_lines += ["DIMS(CMS)"] + [f"{rng.randrange(20, 120)}x{rng.randrange(20, 120)}x{rng.randrange(10, 90)}/{rng.randrange(1, 4)}" for _ in range(rng.randrange(1, 5))]
    goods_lines.append("BY VOL.WT = UNDER.")
    as_prefix = rng.random() < 0.5
    pieces = str(rng.randrange(1, 300))
    charges = rng.choice(["PP", "PP", "PP", "CC"])
    quotes = rng.random() < 0.6
    accounting = ('"FREIGHT PREPAID"' if quotes else "FREIGHT PREPAID") if charges == "PP" else "FREIGHT COLLECT"
    printed = {
        "awb_number": [f"{prefix}-{serial8[:4]} {serial8[4:]}"], "carrier": [carrier],
        "shipper_name": [s_name], "shipper_address": s_addr, "consignee_name": [c_name], "consignee_address": c_addr,
        "agent_name": [f"{rng.choice(AGENTS)}/BKK"], "agent_iata_code": [f"35-3 {rng.randrange(1000, 9999)}"],
        "airport_of_departure": ["SUVARNABHUMI INTERNATIONAL AIRPORT"], "airport_of_destination": [dest_city], "destination_code": [dest_code],
        "first_carrier_code": [code], "flight_date": [f"{code}{rng.randrange(1, 999)}/{flight_day}"], "currency": [rng.choice(["THB", "THB", "USD"])],
        "charges_code": [charges], "declared_value_carriage": [rng.choice(["N.V.D.", "NVD", "N.V.D"])],
        "declared_value_customs": [rng.choice(["N.C.V.", "NCV", "N.C.V"])], "accounting_information": [accounting],
        "pieces_rcp": [pieces], "gross_weight": [f"{gross:.2f}"], "rate_class": ["Q" if q else "M"],
        "chargeable_weight": [("AS " if as_prefix else "") + f"{chargeable:.2f}"], "rate_charge": [f"{rate:.2f}" if q else "MIN"], "total_charge": [money(total)],
        "nature_of_goods": goods_lines, "prepaid_weight_charge": [money(total)], "total_other_charges_agent": [money(agent_ch)],
        "total_other_charges_carrier": [money(carrier_ch)], "total_prepaid": [money(total + agent_ch + carrier_ch)],
        "executed_date": [f"{day:02d}/{month:02d}/{year}"], "executed_place": ["BKK/THAILAND"],
    }
    truth = {k: " ".join(v) for k, v in printed.items() if k != "nature_of_goods"}
    truth["awb_number"] = f"{prefix}-{serial8}"
    truth["chargeable_weight"] = f"{chargeable:.2f}"
    truth["nature_of_goods"] = " ".join(desc)
    truth["hs_code"] = hs
    truth["invoice_reference"] = inv
    if varied:
        vary(truth, rng, "B")
        for fid in VARIED:
            shown = truth[fid]
            printed[fid] = [("AS " if fid == "chargeable_weight" and as_prefix else "") + shown] if shown else []
        if rng.random() < 0.5:
            name = clean(f"{fake.company()} CO., LTD.")[:38]
            printed["agent_name"], truth["agent_name"] = [name], name
    echoes = {"prefix": prefix, "serial8": serial8, "awb_printed": printed["awb_number"][0], "second_flight": f"{code}{rng.randrange(1, 999)}/{rng.randrange(1, 9)}"}
    return printed, truth, echoes


def overlap(i, o) -> float:
    ix = max(0.0, min(i[2], o[2]) - max(i[0], o[0]))
    iy = max(0.0, min(i[3], o[3]) - max(i[1], o[1]))
    a = (i[2] - i[0]) * (i[3] - i[1])
    return ix * iy / a if a else 0.0


def build(rng: random.Random, fake: Faker, base_doc_bytes: bytes, hard: bool = False, varied: bool = False) -> tuple[bytes, dict, dict, dict]:
    doc = fitz.open(stream=base_doc_bytes, filetype="pdf")
    page = doc[0]
    W, H = page.rect.width, page.rect.height
    typed = rng.choices(["hebo", "helv", "cobo", "cour", "tibo"], [0.5, 0.15, 0.15, 0.15, 0.05])[0] if varied else TYPED
    font = fitz.Font(typed)
    spans = [s for b in page.get_text("dict")["blocks"] for ln in b.get("lines", []) for s in ln["spans"] if s["text"].strip()]
    fields = {f.id: f for f in TEMPLATE.fields}
    owned: dict[str, list] = {}
    echo_slots: dict[str, dict] = {}
    for s in spans:
        fr = (s["bbox"][0] / W, s["bbox"][1] / H, s["bbox"][2] / W, s["bbox"][3] / H)
        owner = next((f.id for f in TEMPLATE.fields if overlap(fr, f.bbox) >= 0.5), None)
        t = s["text"].strip()
        if owner:
            owned.setdefault(owner, []).append(s)
        elif t == "176" and fr[0] < 0.1 and fr[1] < 0.02:
            echo_slots["prefix"] = s
        elif re.fullmatch(r"\d{4} \d{4}", t) and fr[1] < 0.02:
            echo_slots["serial"] = s
        elif re.fullmatch(r"\d{3}-\d{4} \d{4}", t) and fr[1] > 0.8:
            echo_slots["awb_bottom"] = s
        elif re.fullmatch(r"[A-Z0-9]{2}\d{1,4}/\d{1,2}", t):
            echo_slots["second_flight"] = s

    printed, truth, echoes = make_values(rng, fake, varied)
    drift = sample_drift(rng, bad_print=0.6, scale=1.8) if hard else sample_drift(rng)

    placements = []  # (origin x, origin y, size, text, zone, field id)
    def place(slot, text, zone, x_limit, fid=None):
        size = slot["size"]
        while font.text_length(text, fontsize=size) > x_limit - slot["origin"][0] and size > 4.6:
            size -= 0.2
        while font.text_length(text, fontsize=size) > x_limit - slot["origin"][0] and " " in text:
            text = text.rsplit(" ", 1)[0]  # does not fit: drop trailing words (and the truth is made to match below)
        placements.append((slot["origin"][0], slot["origin"][1], size, text, zone, fid))
        return text

    for fid, slots in owned.items():
        zone = P.ZONE[fid]
        slots = sorted(slots, key=lambda s: (round(s["origin"][1]), s["origin"][0]))
        lines = printed[fid]
        written = [place(slot, text, zone, fields[fid].bbox[2] * W, fid) for slot, text in zip(slots, lines)]
        if fid in ("shipper_name", "consignee_name", "agent_name", "airport_of_destination", "carrier", "airport_of_departure", "executed_place", "agent_iata_code") and written:
            truth[fid] = written[0]
        if fid in ("shipper_address", "consignee_address"):
            truth[fid] = " ".join(written)
    # the same number the form repeats in other places
    for key, text, zone in (("prefix", echoes["prefix"], "top"), ("serial", f"{echoes['serial8'][:4]} {echoes['serial8'][4:]}", "top"),
                            ("awb_bottom", echoes["awb_printed"], "signing"), ("second_flight", echoes["second_flight"], "routing")):
        if key in echo_slots:
            place(echo_slots[key], text, zone, W * 0.97)

    for s in spans:
        own = any(s is x for v in owned.values() for x in v) or any(s is v for v in echo_slots.values())
        if own:
            page.add_redact_annot(fitz.Rect(s["bbox"]))
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    boxes: dict[str, list[float]] = {}
    for x, y, size, text, zone, fid in placements:
        dx, dy = drift[zone]
        page.insert_text((x + dx * W, y + dy * H), text, fontname=typed, fontsize=size, color=(0, 0, 0))
        if fid:  # where this line ended up, as page fractions: what a crop of the page can or cannot contain
            b = [(x + dx * W) / W, (y + dy * H - 0.78 * size) / H, (x + dx * W + font.text_length(text, fontsize=size)) / W, (y + dy * H + 0.22 * size) / H]
            old = boxes.get(fid)
            boxes[fid] = b if not old else [min(old[0], b[0]), min(old[1], b[1]), max(old[2], b[2]), max(old[3], b[3])]
    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out, truth, {z: [round(dx, 4), round(dy, 4)] for z, (dx, dy) in drift.items()}, {k: [round(c, 4) for c in v] for k, v in boxes.items()}


def scan(pdf: bytes, rng: random.Random, seed: int, hard: bool = False, varied: bool = False) -> bytes:
    img = render_page(fitz.open(stream=pdf, filetype="pdf"), 200)
    if varied:
        img = rough_scan(img, rng, hard)
    if hard:  # a worse scanner: more skew, noise on every page, low contrast and speckle often, heavier compression
        img = skew(img, rng.uniform(-3.0, 3.0))
        img = add_noise(img, rng.uniform(8, 18), seed)
        if rng.random() < 0.6:
            img = contrast(img, rng.uniform(0.5, 0.85), rng.uniform(20, 90))
        if rng.random() < 0.5:
            img = speckle(gamma(img, rng.uniform(1.2, 1.9)), 0.004, seed)
        return image_only_pdf(img, jpeg_quality=rng.choice([45, 55, 70]))
    img = skew(img, rng.uniform(-1.6, 1.6))
    if rng.random() < 0.7:
        img = add_noise(img, rng.uniform(4, 14), seed)
    if rng.random() < 0.35:
        img = contrast(img, rng.uniform(0.55, 0.9), rng.uniform(20, 90))
    if rng.random() < 0.25:
        img = speckle(gamma(img, rng.uniform(1.2, 1.9)), 0.003, seed)
    return image_only_pdf(img, jpeg_quality=rng.choice([55, 70, 88]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "data" / "synthetic_bkk")
    ap.add_argument("--count", type=int, default=50)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--hard", action="store_true", help="worse print misregistration and a worse scanner than the default")
    ap.add_argument("--varied", action="store_true", help="the formats, fonts and scan faults of scripts/synth_formats.py")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    fake = Faker(["en_US", "en_GB", "de_DE", "fr_FR", "nl_NL", "en_IN"])
    Faker.seed(args.seed)
    base = BASE.read_bytes()
    truth_all = {}
    for i in range(args.count):
        for attempt in range(30):
            try:
                pdf, truth, drift, boxes = build(rng, fake, base, args.hard, args.varied)
                break
            except ValueError:
                continue
        name = f"bkk_{i:04d}"
        (args.out / f"{name}.pdf").write_bytes(pdf)
        (args.out / f"{name}__scan.pdf").write_bytes(scan(pdf, rng, args.seed * 1000 + i, args.hard, args.varied))
        truth_all[f"{name}__scan.pdf"] = {"template_id": "air_waybill_bkk", "form": "B", "fields": truth, "drift": drift, "boxes": boxes}
        truth_all[f"{name}.pdf"] = {"template_id": "air_waybill_bkk", "form": "B", "fields": truth, "drift": drift, "boxes": boxes}
    (args.out / "ground_truth.json").write_text(json.dumps(truth_all, indent=1), encoding="utf-8")
    print(f"wrote {args.count} digital + {args.count} scanned documents to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
