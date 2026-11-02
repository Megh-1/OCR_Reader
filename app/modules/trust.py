"""
Audited auto-approval: which fields a person has checked enough to stop checking every value of.

The evidence is the review queue itself. For each (template, version, field) it counts the spot checks a person
resolved ONE AT A TIME - not bulk accepts, which are not an independent look - and how many of those they changed.
A field is trusted when the exact one-sided Clopper-Pearson upper bound on its error rate (app/utils/stats.py) is at
or under TRUST_TARGET_ERROR_RATE. Nothing is learned and nothing is assumed: with no checks the bound is 1, and one
wrong value found among thousands lifts it over the target until enough clean checks pile up again.

Versions are kept apart: a new template version has new boxes, so the old version's checks say nothing about it.
Only the spot checks that could be auto-approved count (TRUSTABLE_WHY, with a value in them), the population the
trust applies to; a value Module 7 rejected is a different population and is left out.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Optional

from sqlalchemy import text

import app.config as config
from app.db import SessionLocal
from app.utils.stats import clopper_pearson_upper

TRUSTABLE_WHY = ("below_auto_threshold", "no_validator")

_NORM = r"regexp_replace(regexp_replace({col}, '^\s+|\s+$', '', 'g'), '\s+', ' ', 'g')"
_COUNTS = text(
    f"""
    SELECT er.template_id, er.template_version, rq.payload->>'field_id' AS field_id,
           count(*) AS checked,
           sum(CASE WHEN {_NORM.format(col="(rq.resolution->>'confirmed_value')")}
                     <> {_NORM.format(col="(rq.payload->>'original_value')")} THEN 1 ELSE 0 END) AS changed
    FROM review_queue rq
    JOIN extraction_results er ON er.id = (rq.payload->>'extraction_result_id')::int
    WHERE rq.status = 'resolved'
      AND rq.entry_type = 'spot_check'
      AND coalesce(rq.resolution->>'via', 'single') = 'single'
      AND rq.payload->>'why' IN ('below_auto_threshold', 'no_validator')
    GROUP BY er.template_id, er.template_version, rq.payload->>'field_id'
    """
)


def evidence_counts(session) -> dict[tuple[str, int, str], tuple[int, int]]:
    """(template_id, version, field_id) -> (checked one at a time, changed by the person)."""
    return {(r.template_id, r.template_version, r.field_id): (int(r.checked), int(r.changed)) for r in session.execute(_COUNTS)}


def is_trusted(checked: int, changed: int, target: Optional[float] = None, confidence: Optional[float] = None) -> bool:
    target = config.TRUST_TARGET_ERROR_RATE if target is None else target
    confidence = config.TRUST_CONFIDENCE if confidence is None else confidence
    return clopper_pearson_upper(changed, checked, confidence) <= target


_cache: dict[tuple[str, int], tuple[float, frozenset[str]]] = {}
_lock = threading.Lock()


def clear_trust_cache() -> None:
    with _lock:
        _cache.clear()


def trusted_fields(template_id: str, version: int) -> frozenset[str]:
    """Field ids of this exact template version that may skip the spot-check queue. Empty when the feature is off."""
    if not config.AUDITED_AUTO_APPROVAL:
        return frozenset()
    now = time.monotonic()
    key = (template_id, version)
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < config.TRUST_CACHE_SECONDS:
            return hit[1]
    with SessionLocal() as session:
        counts = evidence_counts(session)
    trusted = frozenset(f for (t, v, f), (checked, changed) in counts.items() if t == template_id and v == version and is_trusted(checked, changed))
    with _lock:
        _cache[key] = (now, trusted)
    return trusted


def audit_this() -> bool:
    """Whether this one trusted value goes to a person anyway. Random, so what is approved unseen stays measured."""
    return random.random() < config.TRUST_AUDIT_FRACTION
