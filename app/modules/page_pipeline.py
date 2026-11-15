"""
Page-first reading for the pipeline (sprint Step 4): the page engines read the whole page with no template, a matching template adds its own
read as a second reader, and page_tiers decides each field's tier. process_page calls `read_page_first`; everything after it (compile, store,
review queue, export) is the existing code.

Fallbacks, in order: an engine that is down is dropped (the remaining engines still read the page); no page engine answers -> None, and the page
goes down the ordinary template path with its auto values capped at spot_check (it was meant to be read by page engines); a page that is not an
air waybill schema (an ID card template) is never read here.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from app import config
from app.models.text_layer import TextLayer
from app.modules import page_reader
from app.modules.field_extraction import FieldExtractionResult
from app.modules.grounding import page_text
from app.modules.page_tiers import TieredField, tier_fields
from app.modules.template_store import get_latest_template
from app.modules.validation import FieldValidationResult, ValidationSummary
from app.modules.vlm_reader.client import ReaderUnavailable
from app.modules.vlm_reader.engines import EngineSpec, load_registry
from app.schemas.template import TemplateOut

log = logging.getLogger(__name__)

BASE_SCHEMA_ID = "air_waybill"  # judges a page no template matched: its field regexes, check digit and cross-field checks


@dataclass
class PageFirstRead:
    template: TemplateOut  # the schema the fields were judged against
    matched: bool  # a template matched the page (its own read was available as a second reader)
    tiers: dict[str, TieredField]
    summary: ValidationSummary
    primary_engine: str
    engines_used: list[str]
    crop_fields: Optional[dict[str, FieldExtractionResult]]  # the template path's read, when it ran
    crop_review_image: Optional[bytes] = None
    crop_reader: Optional[str] = None


def enabled() -> bool:
    return config.READER_MODE == "page_first" and bool(config.READER_PAGE_ENGINES)


def configured_specs() -> list[EngineSpec]:
    """The configured page engines in READER_PAGE_ENGINES order; those the registry does not know are skipped with a warning."""
    try:
        registry = load_registry()
    except ReaderUnavailable as exc:
        log.warning("page engines unavailable: %s", exc)
        return []
    by_name = {e.name: e for e in registry.engines if e.role == "page" and e.kind == "openai_compatible"}
    specs = []
    for name in config.READER_PAGE_ENGINES:
        if name in by_name:
            specs.append(by_name[name])
        else:
            log.warning("READER_PAGE_ENGINES names %r, which is not a page engine in the engines file", name)
    return specs


def is_awb_schema(template: TemplateOut) -> bool:
    return set(page_reader.FIELD_IDS) <= {f.id for f in template.fields}


def _options(text_layer: Optional[TextLayer]) -> dict:
    layer_text = " ".join(w.text for w in text_layer.words) if text_layer is not None and config.READER_PAGE_LAYER else None
    return dict(layer_text=layer_text, normalise=config.READER_PAGE_NORMALISE, sharper=config.READER_PAGE_SHARPER, halves=config.READER_PAGE_HALVES)


def read_pages(specs: list[EngineSpec], page_bytes: bytes, text_layer: Optional[TextLayer]) -> list[page_reader.PageRead]:
    """Every engine reads the page (in parallel; they are separate model servers or one server taking requests in turn). Never raises."""
    opts = _options(text_layer)

    def one(spec: EngineSpec) -> page_reader.PageRead:
        # OCR parsers (GLM-OCR) are asked in their own wording (a field skeleton); the others get the schema prompt.
        skeleton = spec.prompt_style == "skeleton"
        o = dict(opts, halves=False, layer_text=None) if skeleton else opts
        return page_reader.read_page(page_bytes, spec, skeleton=skeleton, **o)

    with ThreadPoolExecutor(max_workers=max(1, len(specs))) as pool:
        return list(pool.map(one, specs))


def _confidence(t: TieredField) -> float:
    """What the UI shows as confidence for a page-engine value: the share of the three pieces of evidence it has (rules passed, on the page, a
    second reader agrees). 100 = all three; the tier, not this number, is the decision."""
    rules_ok = t.tier != "review" or "readers_disagree" in t.reasons
    return round(100.0 * (bool(rules_ok) + t.grounded + (t.agreed is True)) / 3, 1)


def build_summary(tiers: dict[str, TieredField]) -> ValidationSummary:
    accepted, flagged = {}, {}
    for fid, t in tiers.items():
        res = FieldValidationResult(field_id=fid, value=t.value, confidence=_confidence(t), accepted=t.tier != "review", reasons=list(t.reasons), approval_tier=t.tier)
        (accepted if res.accepted else flagged)[fid] = res
    return ValidationSummary(accepted=accepted, flagged=flagged)


def read_page_first(page_bytes: bytes, text_layer: Optional[TextLayer], matched_template: Optional[TemplateOut], crop_runner, field_image_bytes: bytes) -> Optional[PageFirstRead]:
    """Read the page with the page engines. `crop_runner()` -> PageExtraction of the template path (called only when a template matched and the
    crop engine is on). Returns None when page mode cannot be used for this page (see the module doc)."""
    if matched_template is not None and not is_awb_schema(matched_template):
        return None
    schema = matched_template or get_latest_template(BASE_SCHEMA_ID)
    if schema is None or not is_awb_schema(schema):
        log.warning("page mode: no air waybill schema is available to judge the page")
        return None
    specs = configured_specs()
    if not specs:
        return None
    reads = [r for r in read_pages(specs, page_bytes, text_layer) if r.ok]
    if not reads:
        log.warning("page mode: no page engine answered; the page goes down the template path")
        return None
    primary = reads[0]
    others: dict[str, dict[str, str]] = {r.engine: {fid: f.value for fid, f in r.fields.items()} for r in reads[1:]}
    crop = None
    if matched_template is not None and config.READER_CROP_ENGINE == "template_path":
        crop = crop_runner()
        others[f"template_{crop.reader}"] = {fid: e.raw_text for fid, e in crop.fields.items()}
    page = page_text(field_image_bytes, text_layer, ocr=text_layer is None)
    tiers = tier_fields(schema, {fid: (f.value, f.source_text) for fid, f in primary.fields.items()}, primary.engine, others, page)
    return PageFirstRead(
        template=schema, matched=matched_template is not None, tiers=tiers, summary=build_summary(tiers), primary_engine=primary.engine,
        engines_used=[r.engine for r in reads], crop_fields=crop.fields if crop else None,
        crop_review_image=crop.review_image_bytes if crop else None, crop_reader=crop.reader if crop else None,
    )
