"""
How far the typed layer of a pre-printed form sits from where it should, zone by zone, as page fractions.

Measured on the real scans: the cargo-table row is typically 0.01-0.03 low, the charges block ~0.03 high, the rest within a line or
two. Used by the synthetic generators so that their fakes are misregistered the way the real scans are.
"""

import random

# zone -> (std of dx, std of dy, mean dy)
DRIFT = {"top": (0.004, 0.004, 0.0), "parties": (0.008, 0.012, 0.0), "routing": (0.008, 0.012, 0.0), "table": (0.010, 0.012, 0.012),
         "charges": (0.010, 0.014, -0.010), "signing": (0.008, 0.010, 0.0)}
MAX_DX, MAX_DY = 0.025, 0.04  # never further than the real scans show


def sample_drift(rng: random.Random, bad_print: float = 0.10, scale: float = 1.8) -> dict[str, tuple[float, float]]:
    d = {z: (rng.gauss(0, sx), rng.gauss(m, sy)) for z, (sx, sy, m) in DRIFT.items()}
    if rng.random() < bad_print:  # a document whose print is badly off: values leave their cells
        d = {z: (dx * scale, dy * scale) for z, (dx, dy) in d.items()}
    return {z: (max(-MAX_DX, min(MAX_DX, dx)), max(-MAX_DY, min(MAX_DY, dy))) for z, (dx, dy) in d.items()}


def zone_by_height(y_fraction: float) -> str:
    """for text that belongs to no field (the form's repeated numbers): the zone of the page it sits in"""
    for limit, zone in ((0.06, "top"), (0.26, "parties"), (0.42, "routing"), (0.60, "table"), (0.86, "charges")):
        if y_fraction < limit:
            return zone
    return "signing"
