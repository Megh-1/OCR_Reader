"""
The JSON a person takes away from a batch.

Every value carries WHY it can be trusted, and by default (`strict`) a value nobody has settled is withheld: it is emitted
as null with the machine's reading beside it as `suggested`, never as the answer. That is the project's rule - refuse and
flag rather than emit a wrong value - applied to the output. The emitted values are therefore:

  confirmed   a person confirmed or corrected it on its own
  accepted    a person accepted it together with others (a group, or the agreed values); an unseen accept
  auto        Module 7 approved it with no person (never a vision-model read: those are capped under the threshold)
  unverified  the machine's read, no one has settled it  -> value null in strict mode, `suggested` carries the read

so `summary.emitted_basis` says how much of the file rests on a look and how much on the readers alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from app.db import SessionLocal
from app.models.batch import BatchDocumentRecord, BatchRecord
from app.models.extraction_result import ExtractionResultRecord

router = APIRouter()


def field_status(field: dict) -> str:
    """The provenance of one stored field (see the module docstring)."""
    if field.get("verified_by") == "person":
        return "confirmed"
    if field.get("verified_by") == "bulk":
        return "accepted"
    if field.get("approval_tier") == "auto" and not field.get("needs_review"):
        return "auto"
    return "unverified"


def export_field(field: dict, strict: bool) -> dict:
    status = field_status(field)
    out = {"value": field["value"], "status": status, "confidence": field.get("confidence")}
    if status == "unverified":
        out["reasons"] = field.get("reasons") or []
        if strict:
            out["value"] = None
            out["suggested"] = field["value"]
    return out


@router.get("/documents/batch/{batch_id}/export")
def export_batch(batch_id: str, strict: bool = Query(True)) -> dict:
    with SessionLocal() as session:
        batch = session.get(BatchRecord, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail=f"No batch found for id {batch_id}")
        docs = session.scalars(select(BatchDocumentRecord).where(BatchDocumentRecord.batch_id == batch_id).order_by(BatchDocumentRecord.position)).all()
        result_ids = [p["extraction_result_id"] for d in docs for p in (d.result or {}).get("pages", []) if p.get("extraction_result_id")]
        records = {
            r.id: r
            for r in session.execute(select(ExtractionResultRecord.id, ExtractionResultRecord.template_id, ExtractionResultRecord.template_version, ExtractionResultRecord.template_matched, ExtractionResultRecord.fields).where(ExtractionResultRecord.id.in_(result_ids))).all()
        } if result_ids else {}

        counts = {"confirmed": 0, "accepted": 0, "auto": 0, "unverified": 0}
        documents = []
        for d in docs:
            pages = []
            for page in (d.result or {}).get("pages", []):
                rid = page.get("extraction_result_id")
                rec = records.get(rid)
                if rec is None:
                    pages.append({"recognised": False, "reason": page.get("reason") or "unknown"})
                    continue
                fields = {fid: export_field(f, strict) for fid, f in rec.fields.items()}
                for f in fields.values():
                    counts[f["status"]] += 1
                pages.append(
                    {
                        "recognised": True, "template_id": rec.template_id if rec.template_matched else None,
                        "template_version": rec.template_version if rec.template_matched else None,
                        "reader": page.get("reader"), "result_id": rid,
                        "complete": all(f["status"] != "unverified" for f in fields.values()),
                        "fields": fields,
                    }
                )
            documents.append({"filename": d.filename, "status": d.status, "error": d.error, "pages": pages})

    total = sum(counts.values())
    return {
        "batch_id": batch_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strict": strict,
        "summary": {
            "documents": len(documents),
            "fields": total,
            "emitted": total - counts["unverified"] if strict else total,
            "withheld": counts["unverified"] if strict else 0,
            "emitted_basis": counts,
            "unrecognised_pages": sum(1 for d in documents for p in d["pages"] if not p["recognised"]),
        },
        "documents": documents,
    }
