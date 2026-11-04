"""
Drift: an engine that was good on the scorecard but is being corrected by people more than it was.

The comparison is like with like. A scorecard's overall accuracy counts every field, but a person only ever sees the values that were
NOT auto-approved, which are the harder ones, so live checks are compared with the scorecard's accuracy on the fields that WOULD have
been queued (`queued_accuracy`). Only values a person settled one at a time count (a bulk accept is not an independent check, the same
rule as the trust evidence).
"""

from __future__ import annotations

from app import config


def is_drifted(card, live) -> bool:
    """`card`: selector.Card, `live`: selector.Live. Too few checks say nothing, so they never drift an engine out."""
    if live.checks < config.ENGINE_DRIFT_MIN_CHECKS or card.queued_fields <= 0:
        return False
    return live.unchanged / live.checks < card.queued_accuracy - config.ENGINE_DRIFT_MARGIN
