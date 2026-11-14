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
