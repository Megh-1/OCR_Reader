# Tesseract-only reader: baseline (Step 1)

2026-11-14. No code change between the numbers below and the sources they come from. Measured on the production image (Debian Tesseract 5.5.0), current
source bind-mounted, `VLM_READER_URL` / `READER_ENGINE` empty (`engines.available()` is False, so every scanned or photographed page is read by
Tesseract and reports `reader: "ocr"`; a text-layer PDF reads its layer). Real API path: `POST /documents/batch`, no type hint, 1 worker, then
`export_batch_json.py --wait`. Scoring is the unchanged `same()`; the three scoring scripts hash the same as at the start of the loop.

Three tiers, reported separately. **GOLD first.**

## GOLD (real, 8 documents, aggregate only, labels are UNVERIFIED drafts)

| | value |
|---|---|
| documents identified / refused | 5 / 3 (`no_match`) |
| fields scored (identified pages only) | 165 |
| fields right | 29 (17.6%) |
| by tier: auto / spot_check / review | 13 / 48 / 104 |
| wrong at `auto` | **1** (of 13 auto values) |
| wrong at spot_check / review | 34 / 101 |
| by reader: ocr | 9 of 99 right (1 wrong at auto) |
| by reader: text_layer | 20 of 66 right (0 wrong at auto) |
| speed | 5.3 docs/min (8 documents, 91 s wall) |

A baseline, not a result. The labels are drafts and 3 of the 8 documents are not recognised at all, so most of the gap is Module 5 refusing a layout
it has never seen (3 gold documents are in layouts absent from the dev pool: `SPLIT.json`). **There is one wrong value at `auto` on GOLD.** That
breaks the primary target and is reported as it stands; no per-field diff of it was looked at.

## HOLDOUT (synthetic, sealed, seed 9203, totals only)

50 documents (`--difficulty hardest`), 47 identified (3 `no_match`). 1551 fields scored, **360 right (23.2%)**; tiers auto 210 / spot_check 283 /
review 1058; wrong at auto **16**, at spot_check 141, at review 1034. By kind: digital 236 of 594 right (7 wrong at auto), scan 78 of 462 (3),
photo 46 of 495 (6). Nothing else of it was looked at.

## DEV (synthetic, tunable): `data/demo_50`, seed 7001, regenerated with the clean generator

24 digital / 18 scan / 8 photo, 49 of 50 identified (1 `no_match`). Outcomes: 13 `air_waybill_bkk` read from the text layer + 12 by OCR,
11 `air_waybill` from the text layer + 13 by OCR, 1 refused.

| input kind | fields | right | % | wrong at auto | wrong at spot_check | wrong at review |
|---|---|---|---|---|---|---|
| digital | 792 | 320 | 40.4 | 10 | 72 | 390 |
| scan | 561 | 91 | 16.2 | 6 | 67 | 397 |
| photo | 264 | 22 | 8.3 | 1 | 23 | 218 |
| **all** | 1617 | 433 | 26.8 | 17 | 162 | 1005 |

Tiers: auto 249, spot_check 331, review 1037. (Fields of the one refused document are not in these figures; the taxonomy counts them as ID.)

Cost: 541 s wall for 50 documents = **5.5 docs/min**. Per document (one worker): digital 5.6 s, scan 9.7 s, photo 10.7 s. Field extraction itself
(measured in the taxonomy run) takes ~0 s on a text layer, 3.7 s on a scan and 5.7 s on a photo; the rest of the time is Module 5's anchor OCR and
the whole-page alignment OCR.

### Error taxonomy (every field that is wrong, or right but not auto, gets exactly one cause)

Rule order, first match wins: ID, PRE, VAL, GATE, FMT, ML, LOC, REC, UNK (`scripts/loop_taxonomy.py`).

| cause | digital | scan | photo | all | what it is |
|---|---|---|---|---|---|
| ID | 0 | 33 | 0 | 33 | the page was refused at Module 5 |
| PRE | 0 | 59 | 178 | 237 | fewer than 30% of the document's true words are found by a whole-page OCR: the page is unreadable to Tesseract as rendered |
| LOC | 419 | 187 | 33 | 639 | the value is not in the window the field was read in (misregistered print, loose alignment, a neighbour's cell) |
| REC | 37 | 91 | 6 | 134 | the value is in the window and the read still differs |
| ML | 16 | 4 | 0 | 20 | a multi-line value came back short or cut off |
| FMT | 0 | 14 | 0 | 14 | same characters, different format (`130,242` for `130,242.00`) |
| UNK | 0 | 115 | 25 | 140 | none of the above (9.9% of 1418; limit 10%) |
| VAL | 103 | 22 | 8 | 133 | right, but the field has no validator, so never `auto` |
| GATE | 32 | 31 | 5 | 68 | right, has a validator, still not `auto` (confidence under 90, or a cross-check rejected a right value) |

Reading it: the wrong-or-lost fields are dominated by **LOC on digital pages (419)** and **PRE on photographs (178 of 264 fields)**. Of the 24
digital documents, 19 have the same block of 19 fields (`first_carrier_code`, `flight_date`, `currency`, `charges_code`, `prepaid_weight_charge`,
`total_prepaid`, `airport_of_*`, `declared_value_*`, ...) read from the wrong place. That is the generator's print misregistration (the typed layer
sits off the printed cells), the same failure the vision-reader work found on real pages 05, 10 and 11; the text-layer read takes whatever words sit in
the template's box. On scans, PRE and LOC dominate; the 115 UNK are values that fit none of the rules above and have not been classified further.

### The ten worst (field, kind) pairs (wrong, not counting VAL/GATE)

| field | kind | n | causes |
|---|---|---|---|
| consignee_address | digital | 19 | ML 9, REC 10 |
| first_carrier_code | digital | 19 | LOC 19 |
| flight_date | digital | 19 | LOC 18, REC 1 |
| currency | digital | 19 | LOC 19 |
| charges_code | digital | 19 | LOC 19 |
| prepaid_weight_charge | digital | 19 | LOC 19 |
| total_prepaid | digital | 19 | LOC 19 |
| shipper_address | digital | 18 | ML 7, REC 9, LOC 2 |
| airport_of_departure | digital | 18 | LOC 17, REC 1 |
| airport_of_destination | digital | 18 | LOC 18 |

### Wrong values at `auto` on DEV (17, the number that must be zero)

| document | field | read | truth | cause |
|---|---|---|---|---|
| demo_b_03 digital | total_other_charges_carrier | 501313.15 | 1851.00 | LOC |
| demo_b_04 digital | total_other_charges_carrier | 1215364.00 | 1616.00 | REC |
| demo_b_12 digital | total_other_charges_carrier | 772,006.40 | 3,054.00 | LOC |
| demo_b_06 digital | total_prepaid | 1,023 | 96,411.50 | LOC |
| demo_b_05 digital | airport_of_destination | ORD TG | CHICAGO, USA | LOC |
| demo_m_14 digital | prepaid_weight_charge / total_other_charges_carrier | 5 / 412 | 330,986 / 2913 | LOC |
| demo_m_16, 19, 21 digital | total_other_charges_agent | 1464.00 / 2488.00 / 1229.00 | 128.00 / 191.00 / 661.00 | REC |
| demo_m_01 photo | total_other_charges_agent | 1788.00 | 391.00 | REC |
| demo_m_02 scan | currency, total_charge | CUP, 153.60 | EUR, 171,153.60 | UNK, REC |
| demo_m_05 scan | airport_of_destination | TANBUL | ISTANBUL | REC |
| demo_m_18 scan | prepaid_weight_charge | 63874. | 63874.95 | REC |
| demo_b_14 scan | prepaid_weight_charge | 130,242 | 130,242.00 | FMT |
| demo_b_18 scan | airport_of_destination | DURALUAE | DUBAI,UAE | UNK |

Almost all are in four numeric fields whose regex admits any amount and whose neighbours (other charges, totals) are of the same shape, which is
exactly why a neighbour's value passes. This is a Step 4 (VAL/GATE) target; for the digital pages it is also the LOC lever.

### Hallucination check (`scripts/hallucination_check.py`)

Non-empty answers whose words are on no word of the page (its text layer plus one whole-page OCR), per input kind. Indicators, not proofs.

| kind | answered | blank crop answered | unsupported (word not on the page) | of which at auto | untestable |
|---|---|---|---|---|---|
| digital | 392 | 0 | 13 | 13 | 30 |
| scan | 427 | 7 | 159 | 8 | 93 |
| photo | 205 | 1 | 47 | 5 | 100 |

An unsupported answer at `auto` is a stronger warning than a wrong one: 13 on digital pages means the reader accepted a value that is not on the
page's own text.

## What the baseline says about the plan

1. The tier targets (digital >= 99.5% fields right, scans >= 97%) cannot be reached by tuning a reader: on DEV 40.5% of digital fields are right
   and 419 of the 471 missing ones are LOC. Step 2 (locating) is the whole game; Steps 3 and 4 only matter after it.
2. On a text-layer page the first lever is where the typed words are, not how they are read. The template's box is fixed; the print's offset is not.
3. 178 of 264 photo fields are PRE (the page does not OCR). That is a preprocessing problem (lever d), and the honest target for photos is
   "flag everything doubtful" (the brief makes photos report-only).
4. The 17 wrong-at-auto values are concentrated in four amount fields; cross-field arithmetic (Step 4) is the right tool, not a threshold.
