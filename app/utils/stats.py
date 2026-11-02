"""Exact binomial bounds, for saying how sure we are about an error rate measured on a sample.

No scipy: the project does not depend on it, and the one-sided Clopper-Pearson bound is a short bisection.
"""

from __future__ import annotations

import math


def _binomial_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p), summed in log space so n in the tens of thousands is fine."""
    if p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 1.0 if k >= n else 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    total = 0.0
    for i in range(0, min(k, n) + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q)
    return min(1.0, total)


def clopper_pearson_upper(errors: int, n: int, confidence: float = 0.95) -> float:
    """One-sided upper confidence bound on the true error rate after seeing `errors` wrong values in `n` checked.

    With no errors it is 1 - (1 - confidence) ** (1 / n), the "rule of three" (about 3 / n at 95%).
    Nothing checked says nothing (1.0), never "0 errors so 0%".
    """
    if n <= 0 or errors >= n:
        return 1.0
    alpha = 1.0 - confidence
    lo, hi = 0.0, 1.0
    for _ in range(80):
        mid = (lo + hi) / 2
        # the bound is the p at which seeing this few errors would happen only `alpha` of the time
        if _binomial_cdf(errors, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def clean_checks_needed(target: float, confidence: float = 0.95) -> int:
    """How many checked values with ZERO errors it takes before the upper bound is at or under `target`."""
    if not 0 < target < 1:
        raise ValueError("target must be between 0 and 1")
    return math.ceil(math.log(1.0 - confidence) / math.log1p(-target))
