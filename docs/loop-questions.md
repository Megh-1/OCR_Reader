# Questions raised during the Tesseract loop (written instead of asking; the loop continued)

## 1. The gold set is 8 documents, not 15-20 (2026-11-14)

The brief asks for 15-20 real documents in `data/gold_real/`, with everything else as the dev/style pool. The repository holds **16** real waybills
(`data/real_awb`: Easy 5, Medium 5, Difficult 6). A gold set of 15-20 would leave no dev pool, and the generator bases (real #8, the reference #9),
the landmark lists and the format pool must come from the dev pool. So the split is **8 gold / 8 dev**, drawn as the brief says as far as 16
documents allow (`scripts/make_gold_split.py`, `data/gold_real/SPLIT.json`):

- 4 gold documents are in a layout that keeps documents in the dev pool (Bangkok-print scans 03, 13, 14; Mumbai-print 07), 4 are in a layout
  with none (Pioneer's own form 04, the Indigo print of the Bangkok form 05, SalamAir 15 and 16). Layouts were found by
  `scripts/cluster_real_awb.py` (positional word agreement; plain vocabulary overlap does not separate layouts).
- Difficulty is Easy 1 / Medium 4 / Hard 3, not balanced: the Easy documents are two generator bases (08, 09), a landmark source (10) and 11,
  whose TRAIN-page status in `split.json` put its formats into the synthetic generator; they are dev by necessity.
- Eight documents cannot certify anything. A gold figure is a reality check with a wide interval, and the labels are unverified drafts.

**What would unblock a larger gold set:** more real waybills (any layout), and a second person's sign-off on the labels (`label_verified`).

## 2. The synthetic generator's goods pool contained three descriptions read off gold documents

`scripts/check_gold_isolation.py` found three entries in `scripts/synth_bkk.py` GOODS that were values of gold documents (03, 14, 15). They were
replaced with invented descriptions and `data/demo_50` and `data/holdout_50` were regenerated (holdout re-sealed, new hashes in SEALED.md).
The older synthetic corpora (`data/synthetic_*`) still contain them; they belong to the vision adapter's training and validation and are not
used by this loop.

## 3. Vision-reader exposure of gold documents (not used by the Tesseract path)

03 13 14 07 (and 01) were read per field in the vision reader's error analysis; 04 and 15 were train pages of the vision adapter. No
Tesseract-path asset was derived from them; the landmark lists were rebuilt from the dev pool only (`scripts/landmarks/PROVENANCE.json`).
`tests/test_vlm_reader_replay.py` still reads some gold pages (the vision reader's frozen replay): left intact as instructed.

## 4. The three scoring scripts may not change, but the scorecard needed a hallucination check

The brief fixes the SHA-256 of `export_batch_json.py`, `evaluate_accuracy.py` and `evaluate_reader.py`. The hallucination check is therefore a
separate script, `scripts/hallucination_check.py`, writing `hallucination.json` beside the scorecard. `scripts/evaluate_gold.py` reads gold
in aggregate only.

## 5. Production Tesseract build differs from the Windows one

Windows 5.5.0.20241111 (leptonica 1.85.0) vs the Docker image's Debian 5.5.0 (leptonica 1.84.1); `eng.traineddata` is byte-identical (MD5
d1be414f...). As the brief says, measurement is done in the container (production image, current source bind-mounted, no reader engine:
`engines.available() == False`). The test suite still runs on Windows.
