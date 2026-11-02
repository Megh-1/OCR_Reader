"""
The variety real waybills print and the first synthetic rounds did not, so the model does not learn one format as a habit.

Measured on the real pages: every fake Bangkok page said SUVARNABHUMI INTERNATIONAL AIRPORT, 35-3 NNNN and dd/mm/yyyy and never left a
charge blank, and the model then "read" INT'L as INTERNATIONAL, 35-3-0611 as 35-3 0611 and copied a neighbour into a blank charge. The
formats here are the ones the real TRAIN pages' labels show (data/real_awb/split.json); held-out documents are not a source. Only the
format changes: an amount keeps its value, so weight x rate and the charges sum still hold.
"""

from __future__ import annotations

import random
import re
from decimal import Decimal

import cv2
import numpy as np

from scripts.build_synthetic_corpus import bilevel

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
# the fields vary() may rewrite: a generator copies these back from the dict it passed in
VARIED = ("agent_iata_code", "airport_of_departure", "flight_date", "charges_code", "declared_value_carriage", "declared_value_customs", "gross_weight", "rate_class",
          "chargeable_weight", "rate_charge", "total_charge", "prepaid_weight_charge", "total_other_charges_agent", "total_other_charges_carrier", "total_prepaid",
          "executed_date", "executed_place")
BLANK_CHARGE = 0.15  # 2 of the 14 charge cells on the real train pages are blank
BLANK_CODE = 0.08


def _parse(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _places(x: Decimal) -> int:
    exponent = x.normalize().as_tuple().exponent
    return max(0, -exponent)


def _number(x: Decimal, places: int, commas: bool) -> str:
    return f"{x:,.{places}f}" if commas else f"{x:.{places}f}"


def date_text(rng: random.Random, day: int, month: int, year: int) -> str:
    name = MONTHS[month - 1]
    return rng.choice([f"{day:02d}/{month:02d}/{year}", f"{day:02d}-{name[:3]}-{year}", f"{day:02d}-{name[:3].upper()}-{year}", f"{name} {day}, {year}",
                       f"{name} {day},{year}", f"{name.upper()} {day}, {year}", f"{day} {name[:3].upper()} {year}"])


def _parse_date(text: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", text)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    m = re.fullmatch(r"(\d{2})-([A-Za-z]{3})-(\d{4})", text)
    return int(m.group(1)), [n[:3].upper() for n in MONTHS].index(m.group(2).upper()) + 1, int(m.group(3))


def vary_line(line: str, rng: random.Random) -> str:
    """an address line as clerks type them: no space after a comma, a unit number"""
    if rng.random() < 0.3:
        line = line.replace(", ", ",")
    if rng.random() < 0.15:
        line = f"{rng.randrange(1, 40)}F{rng.choice(['-', ' '])}B{rng.randrange(1, 9)}, {line}"
    return line


def vary(v: dict, rng: random.Random, form: str) -> None:
    """Rewrites the formats of the VARIED fields of one document in place. `form` is B or M."""
    commas = rng.random() < (0.7 if form == "B" else 0.3)
    money_places = rng.choice([2, 2, 2, 0])

    def money(x: Decimal) -> str:
        return _number(x, money_places if x == x.to_integral_value() else 2, commas != (rng.random() < 0.1))

    total = _parse(v["total_charge"])
    agent = None if rng.random() < BLANK_CHARGE else _parse(v["total_other_charges_agent"])
    carrier = None if rng.random() < BLANK_CHARGE else _parse(v["total_other_charges_carrier"])
    v["total_charge"] = money(total)
    v["prepaid_weight_charge"] = money(total)
    v["total_other_charges_agent"] = money(agent) if agent is not None else ""
    v["total_other_charges_carrier"] = money(carrier) if carrier is not None else ""
    v["total_prepaid"] = money(total + (agent or 0) + (carrier or 0))

    for fid in ("gross_weight", "chargeable_weight"):
        x = _parse(v[fid])
        v[fid] = _number(x, max(_places(x), rng.choice([0, 1, 2, 2, 3])), commas and rng.random() < 0.5)
    if v["rate_charge"] != "MIN":
        x = _parse(v["rate_charge"])
        v["rate_charge"] = _number(x, max(_places(x), rng.choice([2, 2, 0])), False)
    if v["rate_class"] == "Q":
        v["rate_class"] = rng.choices(["Q", "N", "K", "C"], [0.65, 0.2, 0.1, 0.05])[0]

    day, month, year = _parse_date(v["executed_date"])
    v["executed_date"] = date_text(rng, day, month, year)
    code, flight, fday = v["first_carrier_code"], rng.randrange(1, 9999), rng.randrange(1, 32)
    mon = MONTHS[rng.randrange(12)][:3].upper()
    if form == "B":
        flight %= 1000
        v["agent_iata_code"] = rng.choice([f"35-3 {rng.randrange(1000):04d}", f"35-3-{rng.randrange(1000):04d}", f"35-3 {rng.randrange(10000):04d} {rng.randrange(10000):04d}",
                                           f"35-3 {rng.randrange(10000):04d} {rng.randrange(1000):03d} {rng.randrange(10)}"])
        v["airport_of_departure"] = rng.choice(["SUVARNABHUMI INTERNATIONAL AIRPORT", "SUVARNABHUMI INT'L AIRPORT", "SUVARNABHUMI AIRPORT,THAILAND", "BANGKOK THAILAND", "BANGKOK", "BKK"])
        v["executed_place"] = rng.choice(["BKK", "BKK/THAILAND", "BANGKOK/THAILAND"])
        v["flight_date"] = rng.choice([f"{code}{flight}/{fday}", f"{code}{flight} /{fday}", f"{code}{flight}/{fday}{mon}"])
    else:
        v["agent_iata_code"] = rng.choice([f"{rng.randrange(10, 99)}/{rng.randrange(100):02d}/{rng.randrange(10000):04d}", f"{rng.randrange(10, 99)}-{rng.randrange(10)}-{rng.randrange(10000):04d}"])
        city = v["executed_place"]
        code_ = re.search(r"\((\w+)\)", v["airport_of_departure"]).group(1)
        v["airport_of_departure"] = rng.choice([f"{city} ({code_})", f"{city}({code_})", city])
        v["flight_date"] = rng.choice([f"{code}{flight:04d}/{fday:02d}{mon}", f"{code} {flight:04d} {fday:02d}/{month:02d}/{year}"])
    if v["declared_value_carriage"].startswith("N"):
        v["declared_value_carriage"] = rng.choice(["NVD", "NVD", "N.V.D.", "N.V.D"])
    if v["declared_value_customs"].startswith("N"):
        v["declared_value_customs"] = rng.choice(["NCV", "NCV", "N.C.V.", "N.C.V", "NVC"])
    if rng.random() < BLANK_CODE:
        v["charges_code"] = ""


def rough_scan(img: np.ndarray, rng: random.Random, hard: bool) -> np.ndarray:
    """what the noise of the first rounds lacked and the real scans show: soft focus, and a page thresholded to black and white"""
    if rng.random() < (0.3 if hard else 0.12):
        img = cv2.GaussianBlur(img, (rng.choice([3, 5]), rng.choice([3, 5])), 0)
    if rng.random() < (0.3 if hard else 0.1):
        img = bilevel(img, rng.randrange(140, 200))
    return img
