"""
Persistence for engine selection: scorecards, pair statistics, promotions, the chain log, the values people settled (labels) and the
live evidence of how an engine is doing. Plain functions over `SessionLocal`, like the other modules' stores.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select, text

from app import config
from app.db import SessionLocal
from app.models.engine_selection import (EngineChainLogRecord, EnginePairRecord, EnginePromotionRecord, EngineScorecardRecord,
                                         LabelledExampleRecord)
from app.modules.engine_selection import selector as S

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- scorecards and pairs ------------------------------------------------------------------------------------------------------

def save_scorecard(template_id: str, card: S.Card, documents: int, by_kind: dict) -> int:
    """Records one measurement. Append-only: the latest row per (template, engine) is the engine's standing."""
    with SessionLocal() as session:
        row = EngineScorecardRecord(
            template_id=template_id, engine=card.engine, measured_at=_now(), corpus_id=card.corpus_id, documents=documents,
            fields=card.fields, right=card.right, wrong_auto=card.wrong_auto, queued_fields=card.queued_fields,
            queued_right=card.queued_right, docs_per_min=card.docs_per_min, by_kind=by_kind,
        )
        session.add(row)
        session.commit()
        new_id = row.id
    S.clear_cache()
    return new_id


def save_pair(template_id: str, pair: S.Pair) -> None:
    a, b = sorted((pair.engine_a, pair.engine_b))
    with SessionLocal() as session:
        session.add(EnginePairRecord(template_id=template_id, engine_a=a, engine_b=b, measured_at=_now(), corpus_id=pair.corpus_id,
                                     fields=pair.fields, agreed=pair.agreed, agreed_right=pair.agreed_right))
        session.commit()
    S.clear_cache()


def latest_cards(template_id: str) -> dict[str, S.Card]:
    with SessionLocal() as session:
        rows = session.execute(select(EngineScorecardRecord).where(EngineScorecardRecord.template_id == template_id)
                               .order_by(EngineScorecardRecord.id)).scalars()
        return {r.engine: S.Card(r.engine, r.corpus_id, r.fields, r.right, r.wrong_auto, r.queued_fields, r.queued_right, r.docs_per_min)
                for r in rows}  # a later row replaces an earlier one


def latest_pairs(template_id: str) -> list[S.Pair]:
    with SessionLocal() as session:
        rows = session.execute(select(EnginePairRecord).where(EnginePairRecord.template_id == template_id)
                               .order_by(EnginePairRecord.id)).scalars()
        latest = {(r.engine_a, r.engine_b, r.corpus_id): S.Pair(r.engine_a, r.engine_b, r.corpus_id, r.fields, r.agreed, r.agreed_right) for r in rows}
        return list(latest.values())


# --- promotions ----------------------------------------------------------------------------------------------------------------

def promoted_engines(template_id: str) -> set[str]:
    with SessionLocal() as session:
        return set(session.execute(select(EnginePromotionRecord.engine).where(EnginePromotionRecord.template_id == template_id)).scalars())


def promote(template_id: str, engine: str, corpus_id: str, numbers: dict) -> None:
    with SessionLocal() as session:
        row = session.execute(select(EnginePromotionRecord).where(EnginePromotionRecord.template_id == template_id,
                                                                 EnginePromotionRecord.engine == engine)).scalar_one_or_none()
        if row is None:
            session.add(EnginePromotionRecord(template_id=template_id, engine=engine, promoted_at=_now(), corpus_id=corpus_id, numbers=numbers))
        else:
            row.promoted_at, row.corpus_id, row.numbers = _now(), corpus_id, numbers
        session.commit()
    S.clear_cache()


def demote(template_id: str, engine: str) -> bool:
    """Removes an engine's promotion (it must earn it again). Returns whether there was one."""
    with SessionLocal() as session:
        row = session.execute(select(EnginePromotionRecord).where(EnginePromotionRecord.template_id == template_id,
                                                                 EnginePromotionRecord.engine == engine)).scalar_one_or_none()
        if row is None:
            return False
        session.delete(row)
        session.commit()
    S.clear_cache()
    return True


# --- live evidence ---------------------------------------------------------------------------------------------------------------

LIVE_SQL = text("""
    SELECT rq.payload->>'original_value' AS original, rq.resolution->>'confirmed_value' AS confirmed
    FROM review_queue rq
    JOIN extraction_results er ON er.id = (rq.payload->>'extraction_result_id')::int
    WHERE rq.status = 'resolved'
      AND rq.entry_type IN ('field_review', 'spot_check')
      AND COALESCE(rq.resolution->>'via', 'single') = 'single'
      AND rq.payload->>'engine' = :engine
      AND er.template_id = :template_id
    ORDER BY rq.resolved_at DESC
    LIMIT :window
""")


def live_stats(template_id: str, engine: str, window: Optional[int] = None) -> S.Live:
    """Of the last `window` values of this engine that a person settled one at a time, how many were left as read (whitespace and
    case are not changes: a reviewer retyping a space is not an engine error)."""
    window = window or config.ENGINE_DRIFT_WINDOW
    with SessionLocal() as session:
        rows = session.execute(LIVE_SQL, {"engine": engine, "template_id": template_id, "window": window}).all()
    norm = lambda v: " ".join((v or "").split()).lower()  # noqa: E731
    return S.Live(checks=len(rows), unchanged=sum(norm(r.original) == norm(r.confirmed) for r in rows))


def load_evidence(template_id: str, reader_names: Iterable[str]) -> S.Evidence:
    cards = latest_cards(template_id)
    evidence = S.Evidence(cards=cards, promoted=promoted_engines(template_id), pairs=latest_pairs(template_id))
    for name in reader_names:
        live = live_stats(template_id, name)
        if live.checks:
            evidence.live[name] = live
    return evidence


# --- the chain log ---------------------------------------------------------------------------------------------------------------

def last_logged_chain(template_id: str) -> Optional[list[str]]:
    with SessionLocal() as session:
        row = session.execute(select(EngineChainLogRecord).where(EngineChainLogRecord.template_id == template_id)
                              .order_by(EngineChainLogRecord.id.desc()).limit(1)).scalar_one_or_none()
        return list(row.chain) if row else None


_last_seen: dict[str, list[str]] = {}


def log_chain_if_changed(decision: S.ChainDecision) -> bool:
    """Writes the decision to the log when it differs from the template's previous one. What is compared is the mode and the template's
    engine RANKING: a page-level difference (a text layer, which needs one reader instead of two) is not a change of choice."""
    key = decision.template_id
    signature = [decision.mode, *decision.ranking]
    if _last_seen.get(key) == signature:
        return False
    previous = last_logged_chain(key)
    _last_seen[key] = signature
    if previous is not None and previous == signature:
        return False
    with SessionLocal() as session:
        session.add(EngineChainLogRecord(template_id=key, at=_now(), chain=signature, mode=decision.mode, numbers={**decision.numbers, "notes": decision.notes}))
        session.commit()
    log.info("template %s: engine chain is now %s (%s); %s", key, decision.ranking or "tesseract only", decision.mode,
             {n: {k: v for k, v in info.items() if k in ("accuracy", "fields", "excluded", "live")} for n, info in decision.numbers.get("engines", {}).items()})
    return True


def chain_log(template_id: str, limit: int = 20) -> list[dict]:
    with SessionLocal() as session:
        rows = session.execute(select(EngineChainLogRecord).where(EngineChainLogRecord.template_id == template_id)
                               .order_by(EngineChainLogRecord.id.desc()).limit(limit)).scalars()
        return [{"at": r.at.isoformat(), "chain": r.chain, "mode": r.mode, "numbers": r.numbers} for r in rows]


# --- labels (what people settled) ---------------------------------------------------------------------------------------------

def record_label(session, template_id: str, template_version: int, result_id: int, field_id: str, value: str,
                 machine_value: Optional[str], engine: Optional[str]) -> None:
    """A value a person settled becomes a label. Part of the caller's transaction (the caller commits). A field settled twice keeps
    its latest value."""
    row = session.execute(select(LabelledExampleRecord).where(LabelledExampleRecord.extraction_result_id == result_id,
                                                              LabelledExampleRecord.field_id == field_id)).scalar_one_or_none()
    if row is None:
        session.add(LabelledExampleRecord(template_id=template_id, template_version=template_version, extraction_result_id=result_id,
                                          field_id=field_id, value=value, machine_value=machine_value, engine=engine, created_at=_now()))
    else:
        row.value, row.created_at = value, _now()


def labels_for(template_id: str) -> list[dict]:
    with SessionLocal() as session:
        rows = session.execute(select(LabelledExampleRecord).where(LabelledExampleRecord.template_id == template_id)
                               .order_by(LabelledExampleRecord.extraction_result_id, LabelledExampleRecord.field_id)).scalars()
        return [{"result_id": r.extraction_result_id, "version": r.template_version, "field_id": r.field_id, "value": r.value,
                 "machine_value": r.machine_value, "engine": r.engine} for r in rows]
