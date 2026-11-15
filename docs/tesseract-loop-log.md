# Tesseract-only loop: log

Started 2026-11-14. Every iteration: hypothesis, change, before/after by input kind, decision. Accept rule: (a) DEV correct fields up (or equal with
fewer wrong at non-auto); (b) zero new wrong at auto on DEV, SANITY (the dev-pool real documents) and the synthetic corpus; (c) no input kind loses
more than 0.2 points; (d) full test suite green; (e) cost rule (per-document time not more than doubled, else behind a flag default OFF).

## Fixed at the start

| item | value |
|---|---|
| `scripts/export_batch_json.py` SHA-256 | C32E07464687171A964BB227810E87A94E006B71DEEE8084D71A86F930225A0E |
| `scripts/evaluate_accuracy.py` SHA-256 | AB2DDBBB2437561141EE471448D20685F6C802B54A2EA022E3193A5E3E3245AE |
| `scripts/evaluate_reader.py` SHA-256 | 18BD568A7E62EC3C2152B8F217FE4713DD82DA44862D08836CDD7EAEF88747AE |
| Tesseract on Windows | 5.5.0.20241111, leptonica 1.85.0; langs: eng, osd |
| Tesseract in the production image (`idp-pipeline:runtime`) | 5.5.0 (Debian), leptonica 1.84.1; langs: eng, osd; `eng.traineddata` MD5 identical to Windows (d1be414f...) |
| Measured in | the production image, current source bind-mounted, `VLM_READER_URL`, `READER_ENGINE`, `READER_ENGINES_FILE` empty, `engines.available() == False` |
| Stack | `docker compose -p idpm` (Postgres 17, Redis 7, app, 1 worker); API http://localhost:8010 |

## Data

| set | what | status |
|---|---|---|
| DEV | `data/demo_50` (seed 7001, 24 digital / 18 scan / 8 photo, 5 injected) + the 22-document corpus in `tests/fixtures/synthetic_awb` | measured freely |
| SANITY | the 8 dev-pool real waybills (01 02 06 08 09 10 11 12); labels are unverified drafts; no change may lower them; never tuned to | per-document allowed |
| GOLD | 8 real waybills in `data/gold_real` (SPLIT.json) | aggregate only (`scripts/evaluate_gold.py`) |
| HOLDOUT | `data/holdout_50` (seed 9203 since the leak fix; `--difficulty hardest`: 18 digital / 14 scan / 18 photo, 8 injected) | sealed; totals only |

`--difficulty hardest` was added to `scripts/build_demo_batch.py`; the default path's output was proved unchanged by content hash (ground truth,
manifest, every page's pixels and text) before and after the option was added, against both a regeneration and the existing `data/demo_50`. PDF
container bytes differ between runs (timestamps), so a raw file hash cannot be used.

## Pre-fix numbers: void

Everything measured before the generator's goods-pool leak was fixed and the sets were regenerated is **void** and not used below: the first
`data/demo_50` smoke run, the first taxonomy smoke test, and the seed-9101 holdout (archived as `data/demo_50_old`, `data/holdout_50_old`, each with
`CONTAMINATED.md`). No change had been accepted before the regeneration, so there was nothing to re-validate.

## Baseline (iteration 0), 2026-11-14, regenerated sets, no code change

Full write-up and taxonomy: `docs/tesseract-baseline.md`. Reported GOLD first, then HOLDOUT, then DEV.

| tier | docs | fields | right | % | auto | wrong at auto | wrong at spot_check | wrong at review | notes |
|---|---|---|---|---|---|---|---|---|---|
| GOLD (real, unverified labels) | 8 (5 identified) | 165 | 29 | 17.6 | 13 | **1** | 34 | 101 | 3 refused `no_match`; 5.3 docs/min |
| HOLDOUT (synthetic, sealed) | 50 (47 identified) | 1551 | 360 | 23.2 | 210 | **16** | 141 | 1034 | digital 236/594, scan 78/462, photo 46/495; wrong auto 7/3/6 |
| DEV `demo_50` | 50 (49 identified) | 1617 | 433 | 26.8 | 249 | **17** | 162 | 1005 | digital 320/792 (40.4%), scan 91/561 (16.2%), photo 22/264 (8.3%); 5.5 docs/min |

DEV cost: 541 s wall for 50 documents; digital 5.6 s/doc, scan 9.7 s/doc, photo 10.7 s/doc. Taxonomy (DEV): LOC 639 (419 on digital), PRE 237
(178 on photos), REC 134, ML 20, FMT 14, ID 33, UNK 140 (9.9%); right-but-not-auto: VAL 133, GATE 68. Hallucination indicators: see the baseline
document. Scoring-script hashes re-checked after the baseline: unchanged.

## Iteration 1: text-layer registration (ACCEPTED, commit e867368)

**Hypothesis.** The 419 LOC fields on digital pages are not reading errors: the typed text layer sits off the printed cells (zone by zone, up to ~0.04
of the page), the label alignment cannot see it, and the template boxes read a neighbour's words. Estimating a per-field shift from geometry plus the
field's own regex should recover them without looking at any value.

**Change.** `app/modules/layer_registration.py` (+ `TEXT_LAYER_REG_*` in `app/config.py`, switch `TEXT_LAYER_REGISTRATION`, now ON). Occupancy of one-line boxes
plus regex passes, over the fields within 0.06 of the page height, on a grid (dy +-0.04 step 0.0025, dx +-0.01 step 0.005); applied only if it beats the
unshifted position by 2. Registered values are capped at `spot_check` and never trusted (`validation.demote_fields`, `pipeline.process_page`).
Offline tries before the accepted one (DEV digital fields right of 792, base 320): word coverage over all boxes 311; wider neighbourhood plus soft overlap
341-347; one-line boxes only, still a single score 33 of 64 on two documents; **one-line occupancy + format 607**.

| DEV (through the API path) | before | after |
|---|---|---|
| digital | 320 / 792 (40.4%) | **607 / 792 (76.6%)** |
| scan | 91 / 561 (16.2%) | 91 / 561 |
| photo | 22 / 264 (8.3%) | 22 / 264 |
| all | 433 / 1617 (26.8%) | **720 / 1617 (44.5%)**; over all 50 x 33 = 1650: 43.6% (was 26.2%) |
| wrong at auto | 17 (digital 10, scan 6, photo 1) | **9** (digital 2, scan 6, photo 1), none new |
| wrong at spot_check / review | 162 / 1005 | 179 / 709 |
| docs/min | 5.5 | 5.4 |

SANITY (4 real text-layer dev-pool waybills): 110 / 132 before and after, no wrong at auto. Synthetic corpus text-layer document: 29 / 29 before and after.
GOLD (aggregate, unverified labels): 29 / 165 -> 32 / 165 right, wrong at auto 1 -> 1 (the OCR reader, not the text layer); over all 8 documents the figure is lower
because 3 are refused. Tests: 650 passed. Accept rules (a)-(e): all met. HOLDOUT not re-run (one accepted change; due after three).

## Iteration 2: wrong values at auto, investigated (NOT applied, nothing committed)

The user's order for the next iteration: break down every wrong-at-auto value by reader path, field, kind and cause and fix the largest cause before any recall
change; report scores over identified documents and over all documents x 33; taxonomy of the 185 remaining failing digital fields.

- DEV after iteration 1, 9 wrong at auto: text_layer 2 (`total_other_charges_carrier`, `prepaid_weight_charge`), ocr 7 (`prepaid_weight_charge` x2, `airport_of_destination` x2,
  `currency`, `total_charge`, `total_other_charges_agent`). Six of nine sit in amount fields with `^[\d.,]{1,12}$`.
- Taxonomy after iteration 1 (DEV): LOC 338, REC 146, ML 20, FMT 16, ID 33, PRE 237, UNK 140; VAL 181 and GATE 364 right-but-not-auto. Digital still wrong: 185 (parties block LOC 16 / 13 / 11 / 9 / 8,
  addresses REC+ML 34, `agent_iata_code` 12).
- Variant vote on the 36 OCR-read auto values (Otsu, and rescaled to ~48 px, agreeing with the first read): caught 2 of 7 wrong, disagreed on 10 right -> rejected.
- Ink touching the window's right edge: flagged 3 of 7 wrong, 7 of 35 right -> rejected. Five of the seven are correlated truncations (`63874.`, `130,242`, `TANBUL`, `153.60`, `1788.00`).
- Not done: the per-reader-path breakdown for GOLD and HOLDOUT (aggregate counts only; HOLDOUT reader per document is in the sealed `extracted.json`), the all-documents x 33 re-scoring of GOLD and HOLDOUT.

## PAUSED 2026-11-15 at the user's request

1 accepted iteration of at most 15; the stop rule has not fired; `docs/tesseract-final-report.md` is not written. The Docker stack `idpm` is down (volumes kept), no
background task is running. Untracked scratch: `scripts/_offs2.py`, `scripts/_oracle.py`, `scripts/_vote_exp.py`. Criteria not met: wrong at auto = 0 (DEV 9, HOLDOUT 16 at baseline,
GOLD 1), digital >= 99.5% (76.6%), scans >= 97% (16.2%).
