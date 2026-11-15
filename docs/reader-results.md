# Reader results (real documents only)

Every table: DEV split of real waybills (01 02 06 08 09 10 11 12; page 1 only), denominator = every document x all 33 fields, refused / failed /
missing = wrong. Scorer: scripts/score_real.py (rules in app/utils/compare.py). Labels are the unverified drafts in data/real_awb/labels/, so
"LABELS UNVERIFIED" heads every table until a person has filled verified_by. DEV has 4 digital PDFs and 4 scans; there is no photo among the real
waybills yet, so the photo row is absent. Nothing here is a synthetic number.

## Baseline (Step 2): the current app, no type hint

Tesseract + text layer + templates, no reader engine (the pre-sprint app, 229 s for 8 documents).

LABELS UNVERIFIED

| current app | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 110 | 83.3 | 70 | 53.0 | 0 | 40 | 0 |
| | scan | 4 | 132 | 27 | 20.5 | 10 | 7.6 | 0 | 17 | 61 |
| | all | 8 | 264 | 137 | 51.9 | 80 | 30.3 | 0 | 57 | 61 |

## Page readers (Step 3): whole page, no template, DEV

Engines served by Ollama on the lab GPU: `page_qwen3vl` = qwen3-vl:8b-instruct (Q4_K_M), `page_glmocr` = glm-ocr. Page image 200 DPI scaled to a long edge of 2000 px
(never up); JSON-schema mode; 120 s timeout; invalid JSON retried once. Tiers are not decided yet (Step 4), so the auto columns read 0 here.

Diagnosis of the 57 misses of the base Qwen read (cause per wrong field, dev only): format 22, label_suspect 16, other 12, small_text 7, missing 0.
Largest buckets: awb_number printed with a space inside (4), airport/goods text spelled or spaced differently (format), `destination_code` read as the
final destination's code instead of the routing's first "To" (5, field_confusion), misreads (rate_class, NVD vs NWD, agent code digits).

Variants (each measured ALONE against the base read, kept only if dev improved; fields right of 264):

| variant | what | Qwen | time per page |
|---|---|---|---|
| base | one whole-page call | 207 | 13-23 s |
| A normalise | deterministic per-type repairs (space inside AWB number, unit word before an amount, "." thousands separator) | 217 | same |
| B sharper | narrower wording for destination_code and flight_date (wording only) | 213 | same |
| C halves | also read the upper and lower part at the page's own resolution, merge by field | 217 | 28-45 s |
| D layer | a digital page's embedded text given as extra help | 210 | 12-19 s |
| A+B+C+D | all kept | **228** | 33-47 s |
| A+B+D | without halves | 226 | 12-21 s |

Not tried (no use on dev): a second pass for missing fields, because the base read left no field missing. label_suspect fields are listed in docs/run-questions.md.

GLM-OCR (same prompt 123 of 264; the OCR-parser wording "output the information in this JSON format" with a field skeleton: G1 144; G1 + normaliser G2 **150**, one page
whose reply was not JSON failed). Its misses are mostly field_confusion (it copies the wrong cell); it is useful as a second reader, not as the primary.

Best variant per engine on DEV (labels UNVERIFIED, page 1 only):

LABELS UNVERIFIED

| page_qwen3vl ABCD | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 119 | 90.2 | 0 | 0.0 | 0 | 119 | 2 |
| | scan | 4 | 132 | 109 | 82.6 | 0 | 0.0 | 0 | 109 | 48 |
| | all | 8 | 264 | 228 | 86.4 | 0 | 0.0 | 0 | 228 | 50 |

| page_glmocr G2 | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 68 | 51.5 | 0 | 0.0 | 0 | 68 | 3 |
| | scan | 4 | 132 | 82 | 62.1 | 0 | 0.0 | 0 | 82 | 42 |
| | all | 8 | 264 | 150 | 56.8 | 0 | 0.0 | 0 | 150 | 45 |

## Tiers per configuration (Step 4), DEV

LABELS UNVERIFIED

| page_qwen3vl alone | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 119 | 90.2 | 0 | 0.0 | 0 | 119 | 2 |
| | scan | 4 | 132 | 109 | 82.6 | 0 | 0.0 | 0 | 109 | 48 |
| | all | 8 | 264 | 228 | 86.4 | 0 | 0.0 | 0 | 228 | 50 |

LABELS UNVERIFIED

| page_glmocr alone | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 68 | 51.5 | 0 | 0.0 | 0 | 68 | 3 |
| | scan | 4 | 132 | 82 | 62.1 | 0 | 0.0 | 0 | 82 | 42 |
| | all | 8 | 264 | 150 | 56.8 | 0 | 0.0 | 0 | 150 | 45 |

LABELS UNVERIFIED

| page_qwen3vl + page_glmocr | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 119 | 90.2 | 59 | 44.7 | 0 | 60 | 2 |
| | scan | 4 | 132 | 109 | 82.6 | 47 | 35.6 | 1 | 63 | 48 |
| | all | 8 | 264 | 228 | 86.4 | 106 | 40.2 | 1 | 123 | 50 |

LABELS UNVERIFIED

| page_qwen3vl + crop (template path) | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 119 | 90.2 | 88 | 66.7 | 0 | 31 | 2 |
| | scan | 4 | 132 | 109 | 82.6 | 15 | 11.4 | 0 | 94 | 48 |
| | all | 8 | 264 | 228 | 86.4 | 103 | 39.0 | 0 | 125 | 50 |

LABELS UNVERIFIED

| page_qwen3vl + page_glmocr + crop | kind | docs | fields | right | % right | auto | % auto | wrong@auto | right-but-flagged | hallucinations |
|---|---|---|---|---|---|---|---|---|---|---|
| | digital | 4 | 132 | 119 | 90.2 | 101 | 76.5 | 0 | 18 | 2 |
| | scan | 4 | 132 | 109 | 82.6 | 52 | 39.4 | 1 | 58 | 48 |
| | all | 8 | 264 | 228 | 86.4 | 153 | 58.0 | 1 | 76 | 50 |

Tier rules (Step 4, app/modules/page_tiers.py): auto = grounded AND rules AND agreement; spot_check = rules pass but grounding or agreement is missing;
review = a rule fails or the readers that read something disagree. Grounded = in the text layer as whole printed words (digital) or fuzzy >= 90 with the
page's own OCR words (scan). Rules = the field regex, the AWB mod-7 check digit and the template's cross-field checks (validation.py, unchanged).
Agreement = another reader gives the same value: the other page engine, or the template path's own read (text layer / Tesseract) when a template matched.

Notes on the table above (page_qwen3vl ABCD, page_glmocr G2, crop = the template path of the baseline run):
- With one reader and no crop engine NOTHING can be auto (the first two rows): that is the rule, not a result.
- The one wrong value at auto in the two configurations with glm is dev doc 06 `total_other_charges_agent`: both page engines read a value, it is on the page,
  and the draft label is BLANK and already flagged uncertain (docs/run-questions.md). If the label is wrong the figure is 0; it was not changed.
- Word-aligned grounding was added after the first live run on DEV showed two wrong values at auto on digital pages (a dropped trailing comma and dropped
  printed quotes, both read by both engines): a plain substring test found them in the text layer. It only ever adds caution (153 auto instead of 154).
- The same configuration without the halves read (A+B+D): 226 right, 158 auto, 1 wrong at auto (the same field).
- Our own adapter (qwen_waybill) as the crop engine was not measured here (see docs/run-report.md).
