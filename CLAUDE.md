# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Coordinate-template document extraction (IDP Phase 1): FastAPI + PostgreSQL backend, React/Fabric.js frontend, Tesseract OCR. `idp-phase1-master-reference.pdf` is the spec every module was built against; its "refuse and flag rather than emit a wrong value" principle governs design choices here.

## Commands

Windows dev machine; use the project venv, not the base conda Python (it lacks `psycopg`).

```bash
.\.venv\Scripts\python.exe -m pytest -q                                # full suite (~30-60s, runs real OCR)
.\.venv\Scripts\python.exe -m pytest tests/test_validation.py::test_matching_regex_and_sufficient_confidence_is_accepted -q   # single test
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000         # backend (NO auto-reload: restart after Python edits)
npm --prefix frontend run dev -- --port 5173                           # frontend
npm --prefix frontend run lint                                         # oxlint (no frontend test runner exists)
.\.venv\Scripts\python.exe -m alembic upgrade head                     # migrations
.\.venv\Scripts\python.exe scripts\seed_templates.py                   # (re)build checked-in templates
.\.venv\Scripts\python.exe scripts\evaluate_accuracy.py --images-dir DIR --ground-truth FILE.json [--sweep]   # per-input-tier accuracy + Module 5 classification
.\.venv\Scripts\python.exe scripts\prune_dev_templates.py [--archive --keep ID ...]   # list (default) / soft-archive non-seeded templates
```

Ad-hoc scripts outside the repo root need `PYTHONPATH=.`, and must set `pytesseract.pytesseract.tesseract_cmd = app.config.TESSERACT_CMD` themselves (only `app/main.py` does it at startup).

## Environment gotchas

- Tests run against a separate `idp_pipeline_test` database: the root `conftest.py` sets `DATABASE_URL` via `setdefault` **before any `app.*` import** (config reads it at import time). Never import `app` above that. The DB must exist; `tests/conftest.py` migrates it to head.
- Port 8000 is hardcoded as the Vite proxy target in `frontend/vite.config.js`. A new backend route prefix needs a proxy entry there or the UI can't reach it. Don't let either server auto-pick a port.
- Tesseract path: `config.TESSERACT_CMD` falls back to the default Windows install path by file check, because PATH edits repeatedly failed to reach spawned processes on this machine.
- Docker files exist but have **never been run** (Docker isn't installed here).
- `tests/fixtures/` is deliberately **untracked**: it holds the owner's personal ID-card photo and an air waybill PDF with real third-party contact data. Never `git add` it without explicit instruction. Tests using the card skip when it's absent.
- `git` prints "LF will be replaced by CRLF" warnings constantly; harmless (verified byte-identical for a binary PDF).

## Architecture

`app/modules/pipeline.py` (`POST /documents/process`) wires the nine spec modules: ingestion → preprocessing → type identification → field extraction → validation → output compiler, with Module 9 (`review_queue.py`) catching both failure kinds (unmatched type → `new_template_needed`; each flagged field → its own `field_review`). Modules are plain functions calling each other's public functions; they do not re-implement each other.

**Two-pass preprocessing is the central design.** Module 2 runs before the document type is known, so `_process_page` preprocesses once generically to identify, then again using the confirmed template's `hints` (`page_aspect_ratio`, `ruled_form` in `schemas/template.py`). Hints default to "behave as before".

**Two renderings per page, identical dimensions** (`ProcessedImage`): `image_bytes` (binarized) is used *only* for Module 5 anchors; `field_image_bytes` (grayscale + CLAHE, optional ruled-line removal) is used for Module 6 and stored for review. Binarization is what makes header text detectable but shreds glyph strokes — never feed one rendering to both consumers.

**Templates** store bboxes as page-relative fractions and are **append-only**: `create_template_version` always INSERTs and template content is never edited or deleted. The one mutable thing is the `is_archived` lifecycle flag (`set_template_archived`); a doc_type is archived when its *latest* version is, so saving a new version un-archives it. Modules 5/6/7 must use the exact confirmed `(template_id, version)` via `get_template_version`, never "latest". `scripts/seed_templates.py` is the canonical source for `bits_goa_id_card` and `air_waybill`; re-seeding adds a version. With no `type_hint`, Module 5 scans every *unarchived* template (`list_known_doc_types`), each costing anchor OCR - the dev DB held ~170 leftover test templates from before test-DB isolation, which `scripts/prune_dev_templates.py` archives after review. A `type_hint` still resolves an archived template.

**Module 5** requires ALL anchors to pass (AND); more than one matching template is `ambiguous_match`, never a pick. Anchors must be printed form labels that are identical on every document of the type, never shipment data, and must not sit close together — each crop is expanded by `ANCHOR_CROP_MARGIN_PX` (40), so adjacent anchors bleed into each other.

**Module 6** reads each field at several Tesseract PSMs (`PSM_CANDIDATES`, declared PSM first and wins ties), discards empty reads, keeps the most confident, and runs fields on a thread pool. Edge-only trimming of rule-fragment characters is the only text repair allowed; content is never "corrected". `Image.load()` must run before the fan-out (PIL lazy decode races otherwise).

**Module 7** flags a field if regex *or* confidence fails, collecting every reason. Enums are validated via `validation_regex` (no separate allowed-values field). Regexes must be as strict as the data: a shape-only date pattern once accepted the OCR misread `17-Oci-2006`.

## Tunables live in `app/config.py` — and are measured

Nearly every constant there carries a comment with the measurement behind it. Don't change one on intuition, and don't tune off a single sample; re-measure. Non-obvious ones: `FIELD_CROP_MARGIN_PX = 0` (tight PDF-derived boxes + any margin drags neighbouring labels in), `OCR_TARGET_LONG_EDGE_PX` (upscaling *hurts* OCR; never shrinks), `PDF_RENDER_DPI = 200` (72 was default and far worse; 300 also worse), `MAX_UPSCALE_FACTOR` (phone photos embed a meaningless 72 DPI tag that once triggered a decompression-bomb crash), `DOC_DETECT_MIN_BORDER_CONTRAST` (without it, a scan's own printed form border is "detected" as the page and the content is cropped away).

## Testing conventions

- Most tests mock `pytesseract`; `tests/test_risk_mitigations.py` runs **real** OCR against the reference doc's risk table. Real-OCR assertions on measured behaviour should stay real.
- `extract_field` calls `image_to_data` once per candidate PSM, so mocks must return one response per mode — use `ocr_responses(...)` from `tests/test_field_extraction.py` (derived from config) rather than hardcoding a call count.
- Tests seed templates from `scripts/seed_templates.py`, not copies, so tests can't drift from what ships.
- `scripts/evaluate_accuracy.py` must measure the real pipeline path, so it calls `pipeline.preprocess_for_template` rather than re-implementing the second pass; keep the two in sync by keeping it that way. Its ground truth for real documents lives in untracked `tests/fixtures/`.
- Extraction accuracy figures in commit messages come from a synthetic "haywire photo" harness (clean render composited on a desk background); real printed/photographed waybills are not yet measured.
