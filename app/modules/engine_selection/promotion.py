"""
Promotion: an engine joins a template's chain only if it beats what is already reading that template, on the same documents.

"What is already reading it" is the best of the template's promoted engines and Tesseract (the floor every chain ends in), each as
measured on the SAME corpus (`corpus_id`) as the candidate. Two scores from different document sets are not comparable, so an
incumbent with no scorecard on the candidate's corpus means "run evaluate_reader.py with the incumbent too", not a pass.

The candidate must also have put no wrong value into `auto` on the corpus, and have been scored on at least ENGINE_MIN_FIELDS fields.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app import config
from app.modules.engine_selection.selector import TESSERACT, Card


@dataclass(frozen=True)
class Verdict:
    promote: bool
    reason: str
    numbers: dict


def judge(candidate: Card, cards: dict[str, Card], promoted: set[str]) -> Verdict:
    """`cards`: the latest scorecard of every engine of the template. Pure."""
    figures = {"candidate": candidate.engine, "accuracy": round(candidate.accuracy, 4), "fields": candidate.fields,
               "wrong_auto": candidate.wrong_auto, "corpus": candidate.corpus_id}
    if candidate.engine == TESSERACT:
        return Verdict(False, "tesseract is the floor of every chain and needs no promotion", figures)
    if candidate.fields < config.ENGINE_MIN_FIELDS:
        return Verdict(False, f"scored on {candidate.fields} fields; at least {config.ENGINE_MIN_FIELDS} are needed", figures)
    if candidate.wrong_auto:
        return Verdict(False, f"{candidate.wrong_auto} wrong value(s) would have reached auto", figures)
    rivals = [c for name, c in cards.items() if name != candidate.engine and (name in promoted or name == TESSERACT) and c.corpus_id == candidate.corpus_id]
    if not rivals:
        return Verdict(False, "no incumbent (a promoted engine, or tesseract) was scored on the same documents; run evaluate_reader.py with it", figures)
    best: Optional[Card] = max(rivals, key=lambda c: c.accuracy)
    figures.update(incumbent=best.engine, incumbent_accuracy=round(best.accuracy, 4), margin=config.ENGINE_PROMOTE_MARGIN)
    if candidate.accuracy >= best.accuracy + config.ENGINE_PROMOTE_MARGIN:
        return Verdict(True, f"beats {best.engine} ({candidate.accuracy:.3f} against {best.accuracy:.3f}) on the same documents", figures)
    return Verdict(False, f"does not beat {best.engine} by {config.ENGINE_PROMOTE_MARGIN:.3f} ({candidate.accuracy:.3f} against {best.accuracy:.3f})", figures)
