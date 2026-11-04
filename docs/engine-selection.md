# Engine selection: decision rules

Which engines read a template's pages, and in what order, is decided by `select_chain(template_id, features)`
(`app/modules/engine_selection/selector.py`). Nobody arranges an order by hand. **The selector is off unless
`ENGINE_SELECTOR_ENABLED=1`**; off, a page is read exactly as before (`VLM_READER_URL` / `READER_ENGINE`, then Tesseract).

## The chain

`[text_layer?] + [model engines, best first] + [tesseract]`

- `text_layer` leads only for a page that has a real text layer. The model sees such a page only when that read is doubtful (existing rule).
- `tesseract` is always last. It is the fallback when every engine before it is unavailable.
- Engines come from the registry (`READER_ENGINES_FILE`: name, kind, url, model, `token_env`, `prompt_file`, timeouts). Registry order never ranks
  anything; it is the last tiebreak between engines equal in every measured respect.

## Which of three modes applies, in this order

1. **override**: `ENGINE_CHAIN_OVERRIDES_FILE` (`{"template_id": ["engine", ...]}`) lists the template. Off by default. Unknown names are ignored with a
   warning. Nothing reaches `auto` unless every engine in the manual chain is also promoted and has a scorecard.
2. **measured**: the template has at least one *promoted* engine that is configured, scored on at least `ENGINE_MIN_FIELDS` (300) fields and not drifted.
   Engines are ranked by field accuracy on their latest scorecard. Engines within `ENGINE_ACCURACY_TIE` (0.005) of the best one in their group are a
   tie, ordered by seconds per document, then registry position. An engine that is not promoted, not measured, too thinly measured or drifted is left out
   (the reason is in the log).
3. **cold start**: no engine qualifies. The configured readers in a fixed prior order (our fine-tuned `service` first, general `openai_compatible` models
   after): one engine if the page has a text layer, otherwise two. The decision carries `force_review=True`: every would-be `auto` value on the page is
   held at `spot_check`, and audited auto-approval (`trust.py`) is not applied. **Nothing is `auto` until an engine has been measured and promoted.**

## Agreement between two readers

| mode | when | effect |
|---|---|---|
| `off` | fewer than two readers | none |
| `caution` | two readers, pair unmeasured / below the bar | a **disagreement** sends that field to review (primary's value kept, confidence 0); agreement changes nothing |
| `trust` | measured mode, pair measured on >= `ENGINE_AGREEMENT_MIN_AGREED` (200) agreed fields with <= `ENGINE_AGREEMENT_MAX_WRONG_RATE` (0.5%) of them wrong | an **agreed** non-empty value counts as gated (`VLM_GATED_CONFIDENCE`, 89) |

Agreement only ever adds caution except in `trust`, and even there the value is a spot check: 89 is under the auto threshold on purpose (see CLAUDE.md).

## Scorecards, promotion, drift

- `scripts/evaluate_reader.py --engine A --engine B --engine tesseract ... --save` stores one scorecard per (template, engine) and the pair statistics.
  Rows are append-only; the latest per (template, engine) is the standing. Every scorecard names its `corpus_id`.
- **Promotion** (`--save --promote`): a candidate is promoted for a template only if, on the **same corpus** as the best current incumbent (promoted
  engines plus Tesseract), it scores at least `ENGINE_PROMOTE_MARGIN` (0.01) higher, put **no wrong value into `auto`**, and was scored on at least
  `ENGINE_MIN_FIELDS` fields. An incumbent with no scorecard on that corpus means "not promotable": run the incumbent in the same command.
- **Drift**: of the last `ENGINE_DRIFT_WINDOW` (200) values of an engine that a person settled one at a time (bulk accepts are not independent checks),
  if at least `ENGINE_DRIFT_MIN_CHECKS` (50) exist and the share left unchanged is below the scorecard's accuracy on queued fields minus
  `ENGINE_DRIFT_MARGIN` (0.05), the engine leaves the chain until it is re-measured and promoted again.
- **Re-ranking** happens whenever the inputs change: a new scorecard, a new or removed engine in the registry, a promotion or demotion, or drift. Evidence
  is cached `selector.CACHE_SECONDS` (30 s).
- **Logging**: every change of a template's chain (mode or engine ranking) is written to `engine_chain_log` and to the application log with the numbers
  (accuracy, fields, speed, live checks, exclusion reasons). A page-level difference (a text layer needing one reader instead of two) is not a change.

## Labels

A value a person settles one at a time (review entry or a correction on Results) is saved in `labelled_examples` (template, version, result, field,
value, the machine's value, the engine). `scripts/export_labelled.py --template ID --out DIR` writes the stored page images and a `ground_truth.json`
that `evaluate_reader.py` accepts, so corrections become the next corpus. The images are the review-size renders of fields that were queued (hard
ones), so a score on them is an *uncertain-field* score, not a representative one. Do not compare it with a scorecard from another corpus.

## What the probe measures, and what it does not

`probe_page` returns: text layer present, sharpness (Laplacian variance at 1200 px wide), contrast, quality `good` / `soft` / `poor` / `unknown`
(thresholds `ENGINE_SHARP_*` are provisional, set on synthetic pages only) and script (only from a text layer). **Language beyond script and
handwriting share are not measured**: handwriting is reported as `None` and no rule reads it. Only the text-layer flag changes the chain today.

## Running it

```bash
.\.venv\Scripts\python.exe -m alembic upgrade head   # migration 0007: scorecards, pairs, promotions, chain log, labels
```

```bash
.\.venv\Scripts\python.exe scripts\evaluate_reader.py --images-dir DIR --ground-truth FILE.json --engine tuned --engine unlimited-ocr --engine tesseract --save --promote
```

Then set `ENGINE_SELECTOR_ENABLED=1` and restart the API and worker (no auto-reload).
