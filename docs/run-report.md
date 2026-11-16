# Sprint run report (branch `sprint-demo`, 2026-11-15/16)

## What changed
* Step 0-1: preflight (`docs/preflight.md`); two pretrained page engines served through Ollama on the lab GPU (own folder `~/idp_sprint`, no sudo):
  `page_qwen3vl` = qwen3-vl:8b-instruct (Q4_K_M, the default `qwen3-vl:8b` tag is the thinking variant and returns nothing), `page_glmocr` = glm-ocr.
  Reached through an ssh tunnel (`scripts/gpu_tunnel.sh`, restarts itself). Servers are left RUNNING. `scripts/gpu_cleanup.sh` exists and was NOT run.
* Step 2: real-document eval sets (dev 8 docs, test sealed), honest scorer (`scripts/score_real.py`: every document x 33 fields, failures count wrong).
* Step 3-4: `app/modules/page_reader.py` (whole page, no template), grounding / agreement / tiers (`page_tiers.py`), page-first mode in `process_page`
  (an unmatched page is read, never refused; `template_id` null in the export), evidence stored per field (engines, source text, grounded, agreed).
* Step 5: default = qwen3vl primary + glmocr + the template's own read, page_first, variants A-D (`docs/reader-results.md`, "Default configuration").
* Step 6: Results shows status + engines + "on the page / readers agree" per field; Review shows the whole page with the source words marked
  (`GET /results/{id}/locate`, display only); Templates and Evidence hidden behind "Advanced tools"; batch card flags a page engine that did not answer.
  `docs/demo-script.md`. `scripts/run_final_test.py` written and unit-tested, NOT run.

## Dev results (8 real docs x 33 fields = 264; labels UNVERIFIED drafts; 4 digital, 4 scan, 0 photos)
| configuration | right | auto | wrong at auto | how |
|---|---|---|---|---|
| app before the sprint | 137 | 80 | see baseline | live |
| qwen3vl alone | 228 | 0 (one reader: nothing can be auto) | 0 | offline |
| qwen3vl + template path | 228 | 110 | 0 | live |
| **default: qwen3vl + glmocr + template path** | **228 (86.4%)** | **153 (58.0%)** | **0** live, 1 on offline replay | live |
By kind (default, live): digital 119/132 right, 107 auto; scan 109/132 right, 46 auto. Hallucinations (value matches nothing on the page): 2 digital, 48 scan.

## Step 7
Not run: `data/eval_real/test/LABELS_VERIFIED` does not exist. **Step 7 waiting for verified labels.** `test/` was never opened.

## Checked live
* Browser flow on 5 real dev AWBs (08-12): upload, batch done in 132 s (3 workers), Results with status + engines, Review with the page and marked source,
  correction saved, Show JSON contains it with `verified_by: person`. The Advanced switch hides/shows Templates.
* One page engine down (pointed at a dead port), one real page: qwen down -> 23 auto (glm + text layer), glm down -> 32 auto, both down -> 33 spot check, 0 auto.
  All completed in 20-30 s and are named in the batch summary.

## Unverified
* All accuracy numbers: the labels are quick drafts (`docs/run-questions.md` lists suspect ones). Doc 06 `total_other_charges_agent` has a blank draft label
  where both models read a value; it is the one wrong-at-auto of the offline replay.
* Scan documents: only 46 of 132 fields are auto. Phone photos: none in the set, so nothing is known about them.
* Highlighting uses a Tesseract pass at view time; when it misreads the printed word, the screen says it could not pinpoint the text.
* Our own Qwen2.5-VL adapter as a crop engine: not measured (needs its weights on the GPU host inside the file quota).

## Broken or awkward
* The app containers cannot reach the ssh tunnel (Docker on WSL2 vs a Windows-side tunnel): the demo stack runs from the Windows venv
  (`scripts/run_local_stack.sh`); Postgres and Redis stay in Docker. Windows Tesseract differs slightly from the container's.
* Throughput: ~45-75 s per page per worker with the halves read; multi-page files (13, 8, 8 pages in dev) take minutes. Demo with single pages.
* Many fields are "readers disagree" (43 waiting for 5 documents): the disagreement is often between a model and the template path's Tesseract/text read.
  Thresholds and comparison rules were not loosened to hide this.

## Open questions
* Who verifies the dev and test labels (and the `agent_name`, `nature_of_goods`, `accounting_information`, `flight_date` conventions)?
* Is ~60 s per page acceptable for the team, or should the halves read (+2 fields, ~2.5x time) be off by default?
* Do we want phone photos in the eval sets before anyone outside the team uses it? Login, roles, audit trail and HTTPS are still required before that.
