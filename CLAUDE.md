# CLAUDE.md

## What we are building

A web application for an airline's cargo team: upload AWBs (PDF, scan, phone photo), get one JSON record per AWB, with as little human review as possible. AWBs first; other document types later as a new schema plus rules on the same reader and review screen.

* Reader: page engines read the whole page with NO template (pretrained Qwen3-VL, GLM-OCR); crop engines (our Qwen2.5-VL LoRA, Tesseract) run only when a template matches. Engines are configuration.
* Checks: grounding (value is on the page), AWB rules (mod-7, formats, cross-field), agreement of two readers. auto = grounded AND rules AND agreement. Otherwise spot_check or review.
* App: upload, Results, Review (page with source highlighted), strict JSON export. Login, roles, audit trail and HTTPS are required before anyone outside the team uses it.

## How we measure (non-negotiable)

* Real documents only for any reported number; synthetic documents are for unit tests only.
* Every document x every schema field counts; refused, failed, missing or wrong-type = wrong.
* Report by input kind: fields right, % auto, wrong at auto, hallucinations.
* data/eval_real/test/ is never opened, printed or tuned on; only scripts/run_final_test.py runs on it, once.
* Tune on data/eval_real/dev/ only. Labels come from people reading the image, never from model output.
* Leakage = any information from a test document reaching code, constants, prompts, data or a decision. If you suspect it, write it in docs/run-questions.md and stop that line of work.

## How we work

* Fixed-scope tasks with time boxes; no self-directed improvement loops; never lower a threshold or change the scorer to raise a number.
* The GPU host is shared: work only in ~/idp_sprint/, never touch others' processes or files, no sudo.
* Keep this file under ~100 lines; history goes in docs/history/.

## Things that bite

* Tests use the separate idp_pipeline_test database (root conftest.py sets DATABASE_URL before any app import).
* A new backend route prefix needs an entry in both frontend/vite.config.js and frontend/nginx.conf.
* Inside Docker, localhost is the container: reach host tunnels via host.docker.internal.
* OMP_THREAD_LIMIT=1 for Tesseract in containers (runtime.configure_tesseract does it).
* PDF invisible text (render mode 3) is OCR output, not a real text layer.
* tests/fixtures/ and data/ hold real documents: never commit them.
