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
