"""
Module 9, the part that keeps the queue short without lowering accuracy.

Three things, all built on the queue rows Module 9 already writes:

* groups: pending entries gathered so one look settles many - the same value read in the same field on many
  documents ("value"), or every pending field of one document ("document").
* bulk accept: confirm many spot checks at once. The SERVER decides what may be bulk-accepted (only a spot check
  with a value in it - never something Module 7 rejected, never "nothing found"), and it holds a random sample of
  every bulk accept back for an individual look, so what was accepted unseen is also measured.
* evidence: of the values a person checked one at a time, how many were right, and the exact upper bound on the
  machine's true error rate. This is what has to be shown before a field is trusted without a person.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from types import SimpleNamespace
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

import app.config as config
from app.db import SessionLocal
from app.models.extraction_result import ExtractionResultRecord
from app.models.review_queue import ReviewQueueRecord
from app.modules import trust
from app.modules.review_queue import VALUE_ENTRY_TYPES, apply_confirmed_value
from app.schemas.review_queue import FieldReviewPayload, SpotCheckPayload
from app.utils.stats import clean_checks_needed, clopper_pearson_upper

router = APIRouter()

BULK_ELIGIBLE_WHY = trust.TRUSTABLE_WHY
MIN_AUDIT_FRACTION = 0.05  # a bulk accept always leaves at least this share (and at least one entry) for a person
DEFAULT_AUDIT_FRACTION = 0.10


def _norm(value: str) -> str:
    return " ".join(value.split())


def bulk_refusal(row: Optional[ReviewQueueRecord]) -> Optional[str]:
    """Why this row may not be bulk-accepted, or None when it may."""
    if row is None:
        return "not_found"
    if row.status != "pending":
        return "not_pending"
    if row.entry_type != "spot_check":
        return "not_a_spot_check"  # field_review: Module 7 rejected it, so a person has to look at (or fix) the value
    payload = SpotCheckPayload.model_validate(row.payload)
    if payload.audit:
        return "audit_sample"  # held back from an earlier bulk accept precisely so a person checks it; never re-bulked
    if payload.why not in BULK_ELIGIBLE_WHY:
        return f"needs_a_look:{payload.why}"
    if not payload.original_value.strip():
        return "empty_value"
    return None


class BulkRefusedError(Exception):
    def __init__(self, refused: dict[int, str]):
        super().__init__(f"{len(refused)} entries cannot be bulk-accepted")
        self.refused = refused


class BulkAcceptRequest(BaseModel):
    entry_ids: list[int] = Field(min_length=1, max_length=2000)
    audit_fraction: float = Field(default=DEFAULT_AUDIT_FRACTION, ge=MIN_AUDIT_FRACTION, le=0.5)


def bulk_accept(entry_ids: list[int], audit_fraction: float = DEFAULT_AUDIT_FRACTION, rng: Optional[random.Random] = None) -> dict:
    ids = list(dict.fromkeys(entry_ids))
    rng = rng or random.Random()
    with SessionLocal() as session:
        rows = {r.id: r for r in session.execute(select(ReviewQueueRecord).where(ReviewQueueRecord.id.in_(ids))).scalars()}
        refused = {i: reason for i in ids if (reason := bulk_refusal(rows.get(i))) is not None}
        if refused:
            raise BulkRefusedError(refused)  # all or nothing: no partial bulk accept

        held = set(rng.sample(ids, max(1, math.ceil(audit_fraction * len(ids))))) if len(ids) >= 2 else set()
        for i in ids:
            row = rows[i]
            if i in held:
                row.payload = {**row.payload, "audit": True}
            else:
                apply_confirmed_value(session, row, SpotCheckPayload.model_validate(row.payload).original_value, via="bulk")
        session.commit()
    return {"accepted": [i for i in ids if i not in held], "held_for_audit": sorted(held)}


@router.post("/review/bulk")
def post_bulk_accept(body: BulkAcceptRequest) -> dict:
    try:
        return bulk_accept(body.entry_ids, body.audit_fraction)
    except BulkRefusedError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc), "refused": {str(k): v for k, v in exc.refused.items()}})


# ---------------------------------------------------------------------------------------------------------
# groups


def _pending_value_rows(session) -> list[ReviewQueueRecord]:
    stmt = select(ReviewQueueRecord).where(ReviewQueueRecord.status == "pending", ReviewQueueRecord.entry_type.in_(VALUE_ENTRY_TYPES))
    return list(session.execute(stmt).scalars())


def _results_by_id(session, ids: set[int]) -> dict[int, SimpleNamespace]:
    """id -> (template_id, template_version). Only those columns: a row's page image is hundreds of KB."""
    if not ids:
        return {}
    stmt = select(ExtractionResultRecord.id, ExtractionResultRecord.template_id, ExtractionResultRecord.template_version).where(
        ExtractionResultRecord.id.in_(ids)
    )
    return {row.id: SimpleNamespace(template_id=row.template_id, template_version=row.template_version) for row in session.execute(stmt)}


def _value_groups(rows, results) -> list[dict]:
    buckets: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if bulk_refusal(r) is not None:
            continue
        p = SpotCheckPayload.model_validate(r.payload)
        res = results.get(p.extraction_result_id)
        if res is None:
            continue
        buckets[(res.template_id, res.template_version, p.field_id, _norm(p.original_value))].append((r, p))
    groups = []
    for (template_id, version, field_id, value), members in buckets.items():
        if len(members) < 2:
            continue  # one is not a group
        members.sort(key=lambda m: m[0].id)
        groups.append(
            {
                "kind": "value",
                "template_id": template_id,
                "template_version": version,
                "field_id": field_id,
                "value": value,
                "count": len(members),
                "min_confidence": min(p.confidence for _, p in members),
                "entry_ids": [r.id for r, _ in members],
                "result_ids": [p.extraction_result_id for _, p in members],
            }
        )
    groups.sort(key=lambda g: (-g["count"], g["template_id"], g["field_id"], g["value"]))
    return groups


def _document_groups(rows, results) -> list[dict]:
    by_doc: dict[int, list] = defaultdict(list)
    for r in rows:
        by_doc[r.payload["extraction_result_id"]].append(r)
    groups = []
    for result_id, members in by_doc.items():
        res = results.get(result_id)
        if res is None:
            continue
        members.sort(key=lambda r: r.id)
        entries = []
        for r in members:
            payload = (FieldReviewPayload if r.entry_type == "field_review" else SpotCheckPayload).model_validate(r.payload)
            entries.append(
                {
                    "id": r.id,
                    "entry_type": r.entry_type,
                    "field_id": payload.field_id,
                    "original_value": payload.original_value,
                    "confidence": payload.confidence,
                    "reasons": getattr(payload, "reasons", []),
                    "why": getattr(payload, "why", None),
                    "audit": getattr(payload, "audit", False),
                    "bulk_ok": bulk_refusal(r) is None,
                }
            )
        groups.append(
            {
                "kind": "document",
                "result_id": result_id,
                "template_id": res.template_id,
                "template_version": res.template_version,
                "count": len(entries),
                "needs_person": sum(1 for e in entries if not e["bulk_ok"]),
                "entries": entries,
            }
        )
    # documents that need a person first, then the ones that are only spot checks, biggest first
    groups.sort(key=lambda g: (-g["needs_person"], -g["count"], g["result_id"]))
    return groups


@router.get("/review/groups")
def review_groups(
    by: str = Query("value", pattern="^(value|document)$"),
    template_id: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    with SessionLocal() as session:
        rows = _pending_value_rows(session)
        results = _results_by_id(session, {r.payload["extraction_result_id"] for r in rows})
    groups = _value_groups(rows, results) if by == "value" else _document_groups(rows, results)
    if template_id is not None:
        groups = [g for g in groups if g["template_id"] == template_id]
    return {"by": by, "total": len(groups), "groups": groups[offset : offset + limit]}


# ---------------------------------------------------------------------------------------------------------
# evidence


@router.get("/review/evidence")
def review_evidence(target: Optional[float] = Query(None, gt=0, lt=1), confidence: Optional[float] = Query(None, gt=0.5, lt=1)) -> dict:
    """Per template version and field: spot checks a person resolved ONE AT A TIME, how many they changed, and the
    upper bound on the machine's true error rate. Bulk accepts are left out - they were not an independent check.
    `trusted` is what audited auto-approval acts on (app/modules/trust.py)."""
    target = config.TRUST_TARGET_ERROR_RATE if target is None else target
    confidence = config.TRUST_CONFIDENCE if confidence is None else confidence
    with SessionLocal() as session:
        counts = trust.evidence_counts(session)

    needed = clean_checks_needed(target, confidence)
    fields = []
    for (template_id, version, field_id), (checked, changed) in sorted(counts.items()):
        upper = clopper_pearson_upper(changed, checked, confidence)
        fields.append(
            {
                "template_id": template_id,
                "version": version,
                "field_id": field_id,
                "checked": checked,
                "changed": changed,
                "observed_error_rate": changed / checked,
                "upper_bound": upper,
                "meets_target": upper <= target,
                "more_clean_checks_needed": max(0, needed - checked) if changed == 0 else None,
            }
        )
    return {
        "target": target,
        "confidence": confidence,
        "clean_checks_needed": needed,
        "audited_auto_approval": config.AUDITED_AUTO_APPROVAL,
        "audit_fraction": config.TRUST_AUDIT_FRACTION,
        "fields": fields,
    }
