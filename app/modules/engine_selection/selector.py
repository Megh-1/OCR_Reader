"""
select_chain(template_id, features) -> ChainDecision: which engines read this template's pages, in order.

The rules are written down in docs/engine-selection.md; this file is them as code. In short:

  override   a manual chain for the template (config.ENGINE_CHAIN_OVERRIDES_FILE; off unless the file lists the template)
  measured   the engines PROMOTED for the template (they beat the incumbent on a shared corpus, promotion.py) that are configured, not
             drifted (drift.py) and scored on enough fields, ranked by field accuracy, ties (within ENGINE_ACCURACY_TIE) by speed
  cold start no promoted engine yet: the configured readers in a fixed prior order (fine-tuned first, general models after), nothing
             reaches `auto` (force_review)

`text_layer` leads the chain of a page that has a text layer and `tesseract` is ALWAYS last. Registry order never decides a ranking: it is
only the final tiebreak between engines that are equal in every measured respect, so nobody has to arrange the file.

`select_chain` is pure (the registry and the evidence are arguments); store-backed loading, logging of chain changes and the cache are
in `select_chain_for`.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from app import config
from app.modules.engine_selection import drift as D
from app.modules.engine_selection.probe import DocFeatures
from app.modules.vlm_reader import engines as E

log = logging.getLogger(__name__)

TESSERACT = "tesseract"
TEXT_LAYER = "text_layer"


@dataclass(frozen=True)
class Card:
    """The latest scorecard of one engine on one template (store.EngineScorecardRecord, less the history)."""

    engine: str
    corpus_id: str
    fields: int
    right: int
    wrong_auto: int
    queued_fields: int
    queued_right: int
    docs_per_min: float

    @property
    def accuracy(self) -> float:
        return self.right / self.fields if self.fields else 0.0

    @property
    def queued_accuracy(self) -> float:
        return self.queued_right / self.queued_fields if self.queued_fields else 0.0

    @property
    def seconds_per_doc(self) -> float:
        return 60.0 / self.docs_per_min if self.docs_per_min > 0 else math.inf


@dataclass(frozen=True)
class Pair:
    engine_a: str
    engine_b: str
    corpus_id: str
    fields: int
    agreed: int
    agreed_right: int

    @property
    def wrong_rate(self) -> float:
        return (self.agreed - self.agreed_right) / self.agreed if self.agreed else 1.0


@dataclass(frozen=True)
class Live:
    """What people found when they checked this engine's values after the scorecard: `checks` settled one at a time, of which
    `unchanged` were left as the engine read them."""

    checks: int
    unchanged: int


@dataclass
class Evidence:
    cards: dict[str, Card] = field(default_factory=dict)
    promoted: set[str] = field(default_factory=set)
    pairs: list[Pair] = field(default_factory=list)
    live: dict[str, Live] = field(default_factory=dict)


@dataclass
class ChainDecision:
    template_id: str
    chain: list[str]  # every step in order, `text_layer` first when the page has one, `tesseract` last
    readers: list[str]  # the model engines among them, in order
    mode: str  # override | measured | cold_start
    force_review: bool  # nothing on this page may reach `auto`
    agreement: str  # off | caution | trust: what two readers agreeing may do (see agreement.py)
    numbers: dict = field(default_factory=dict)  # the figures behind the choice, for the log
    notes: list[str] = field(default_factory=list)
    ranking: list[str] = field(default_factory=list)  # the template's engines in order, whatever this page's own limits (what the log records)

    def as_dict(self) -> dict:
        return asdict(self)


def _configured(spec: E.EngineSpec) -> bool:
    return bool(spec.url) and (spec.kind != "openai_compatible" or bool(spec.model))


def _agreement(readers: list[str], mode: str, evidence: Evidence) -> tuple[str, dict]:
    """`off` with fewer than two readers. Two readers always route a DISAGREEMENT to review (that only adds caution); their
    agreement raises a value's trust only when the pair was measured, on enough fields, to be wrong rarely when they agree."""
    if len(readers) < 2:
        return "off", {}
    a, b = sorted(readers[:2])
    pairs = [p for p in evidence.pairs if (p.engine_a, p.engine_b) == (a, b)]
    if mode == "measured" and pairs:
        p = max(pairs, key=lambda p: p.fields)  # the measurement on the most documents
        numbers = {"pair": [a, b], "agreed": p.agreed, "wrong_rate": round(p.wrong_rate, 4), "corpus": p.corpus_id}
        if p.agreed >= config.ENGINE_AGREEMENT_MIN_AGREED and p.wrong_rate <= config.ENGINE_AGREEMENT_MAX_WRONG_RATE:
            return "trust", numbers
        return "caution", numbers
    return "caution", {"pair": [a, b], "measured": False}


def _rank(scored: list[tuple[Card, str]], order: dict[str, int]) -> list[str]:
    """By accuracy, best first; engines within ENGINE_ACCURACY_TIE of the best one in their group are a tie, ordered by speed, then by
    registry position. Groups are cut from the leader (not at fixed accuracy steps, which would split two engines 0.002 apart whenever
    a step boundary fell between them)."""
    pool = sorted(scored, key=lambda cn: (-cn[0].accuracy, order[cn[1]]))
    out: list[str] = []
    while pool:
        leader = pool[0][0].accuracy
        group = [cn for cn in pool if leader - cn[0].accuracy <= config.ENGINE_ACCURACY_TIE]
        pool = pool[len(group):]
        out += [n for _, n in sorted(group, key=lambda cn: (cn[0].seconds_per_doc, order[cn[1]]))]
    return out


def select_chain(template_id: str, features: DocFeatures, specs: list[E.EngineSpec], evidence: Evidence,
                 overrides: Optional[dict] = None) -> ChainDecision:
    readers_by_name = {s.name: s for s in specs if s.is_reader and _configured(s)}
    order = {s.name: i for i, s in enumerate(specs)}
    lead = [TEXT_LAYER] if features.has_text_layer else []
    numbers: dict = {"features": features.as_dict(), "engines": {}}
    notes: list[str] = []

    # --- 1. a manual chain, when the template has one -------------------------------------------------------------------------
    manual = (overrides or {}).get(template_id)
    if manual:
        known = [n for n in manual if n in readers_by_name]
        if known:
            promoted_all = all(n in evidence.promoted and n in evidence.cards for n in known)
            agreement, anum = _agreement(known, "override", evidence)
            numbers["agreement"] = anum
            notes.append(f"manual chain {known}" + ("" if len(known) == len(manual) else f" (ignored unknown engines {[n for n in manual if n not in known]})"))
            return ChainDecision(template_id, lead + known + [TESSERACT], known, "override", force_review=not promoted_all,
                                 agreement=agreement, numbers=numbers, notes=notes, ranking=known)
        log.warning("template %s: the manual chain %s names no configured engine; choosing automatically", template_id, manual)
        notes.append(f"manual chain {manual} ignored: no such configured engine")

    # --- 2. measured: promoted engines, ranked ---------------------------------------------------------------------------------
    scored: list[tuple[Card, str]] = []
    for name, spec in readers_by_name.items():
        card = evidence.cards.get(name)
        info: dict = {"promoted": name in evidence.promoted}
        if card is not None:
            info.update(accuracy=round(card.accuracy, 4), fields=card.fields, wrong_auto=card.wrong_auto, seconds_per_doc=round(card.seconds_per_doc, 2),
                        corpus=card.corpus_id)
        numbers["engines"][name] = info
        if name not in evidence.promoted:
            info["excluded"] = "not promoted" if card is not None else "not measured"
            continue
        if card is None or card.fields < config.ENGINE_MIN_FIELDS:
            info["excluded"] = f"scored on fewer than {config.ENGINE_MIN_FIELDS} fields"
            continue
        live = evidence.live.get(name)
        if live is not None:
            info["live"] = {"checks": live.checks, "accuracy": round(live.unchanged / live.checks, 4) if live.checks else None}
            if D.is_drifted(card, live):
                info["excluded"] = "drifted"
                notes.append(f"{name} left the chain: live accuracy {live.unchanged}/{live.checks} against {card.queued_accuracy:.3f} on its scorecard")
                continue
        scored.append((card, name))
    if scored:
        readers = _rank(scored, order)
        agreement, anum = _agreement(readers, "measured", evidence)
        numbers["agreement"] = anum
        return ChainDecision(template_id, lead + readers + [TESSERACT], readers, "measured", force_review=False, agreement=agreement,
                             numbers=numbers, notes=notes, ranking=readers)

    # --- 3. cold start: a default chain from the probe, never auto -------------------------------------------------------------
    candidates = sorted(readers_by_name.values(), key=lambda s: (s.prior, order[s.name]))
    # A page that has a text layer is read from it; the model only sees it when that read is doubtful, so one engine is enough. A page
    # read from pixels gets a second engine, whose answer is a check on the first (disagreement goes to review).
    limit = 1 if features.has_text_layer else 2
    ranking = [s.name for s in candidates]
    readers = ranking[:limit]
    agreement, anum = _agreement(readers, "cold_start", evidence)
    numbers["agreement"] = anum
    notes.append("cold start: no engine is promoted for this template; every value is held at spot_check or review")
    return ChainDecision(template_id, lead + readers + [TESSERACT], readers, "cold_start", force_review=True, agreement=agreement,
                         numbers=numbers, notes=notes, ranking=ranking)


# --- the store-backed wrapper ----------------------------------------------------------------------------------------------------

_cache: dict[str, tuple[float, Evidence]] = {}
CACHE_SECONDS = 30.0


def clear_cache() -> None:
    _cache.clear()


def load_overrides() -> dict:
    path = config.ENGINE_CHAIN_OVERRIDES_FILE
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(v, list) and all(isinstance(n, str) for n in v) for v in data.values()):
            raise ValueError("must map a template id to a list of engine names")
        return data
    except (OSError, ValueError) as exc:
        log.warning("the manual chain file %s cannot be used (%s); choosing automatically", path, exc)
        return {}


def select_chain_for(template_id: str, features: DocFeatures) -> ChainDecision:
    """The decision for one page, from the registry and the stored evidence. Every CHANGE of a template's chain is logged, with the
    numbers behind it, to the application log and to the chain log table."""
    from app.modules.engine_selection import store  # imported here: the pure rules above need no database

    specs = E.load_registry().engines
    now = time.monotonic()
    hit = _cache.get(template_id)
    if hit is not None and now - hit[0] < CACHE_SECONDS:
        evidence = hit[1]
    else:
        evidence = store.load_evidence(template_id, [s.name for s in specs if s.is_reader])
        _cache[template_id] = (now, evidence)
    decision = select_chain(template_id, features, specs, evidence, load_overrides())
    store.log_chain_if_changed(decision)
    return decision
