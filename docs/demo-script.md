# Demo script: five real air waybills, upload to corrected JSON

Audience: the cargo team. Length: about 6 minutes of talking plus 2 minutes of waiting for the batch.
Tested on 2026-11-16 in a browser against the stack below (the built-in browser, 5 real dev documents `08`-`12`).

## Before you start (5 minutes, once)

1. Postgres and Redis (Docker, WSL): `docker compose up -d postgres redis` (ports 5433 / 6380).
2. API, 3 workers and the GPU tunnel from the Windows venv: `scripts/run_local_stack.sh start 3` (why not the app containers: `docs/run-report.md`).
3. The two page engines answer through the tunnel: `curl http://127.0.0.1:11434/api/tags` lists `qwen3-vl:8b-instruct` and `glm-ocr`.
4. Console: `npm --prefix frontend run dev -- --port 5173`, open http://localhost:5173.
5. Files to upload: `data/eval_real/dev/08.pdf` ... `12.pdf` (real waybills, one page each; three on the Bangkok form, two standard).
   Do NOT use `01`, `02`, `06`: they have 13 / 8 / 8 pages and take 6-10 minutes each.

The sidebar shows Upload, Results and Review. Templates and the Evidence tab are hidden on purpose; the "Advanced tools" switch at the bottom of the sidebar shows them.

## The flow

| # | Click | Say | Expect |
|---|---|---|---|
| 1 | Upload. Drag the five PDFs onto the drop area (or Choose files). Leave "Detect automatically". Click **Process 5 documents**. | "No template, no document type to choose. Two pretrained vision models read each whole page." | The batch card appears: 5 documents, 3 in progress. |
| 2 | Watch the batch card. | "Each page is read by two models and by the standard reader; the three have to agree." | About **2 minutes 10 seconds** for the five (measured 132 s with 3 workers; 45-75 s per page per worker). |
| 3 | **Results**. Open the newest document. | "Every field has a status. Green is approved without a person." | 33 fields; each row shows the status and under it which reader(s) read it, and "on the page" / "readers agree". Typical: 15-31 auto, the rest spot check or review. |
| 4 | Point at a **Review** row (for example "readers disagree"). | "When the readers disagree, or a format or check digit fails, a person looks. Nothing is approved on one model's word." | Red row with the reason in words. |
| 5 | **Review** (left bar) > **This upload** > **One by one**. | "A person sees the whole page, with the printed words the value came from marked." | The full page with a box on the source text. If the model's value is not printed on the page (a code inferred from a city name) there is no box and the screen says so. |
| 6 | Press **C**, type the right value, **Save correction**. | "One correction, one keystroke." | The queue count drops by one ("1 corrected"). |
| 7 | **Results**, open the same document, **Show JSON** (or **Download**). | "This is the export. The correction is in it, marked as verified by a person." | The field has the new value and `"verified_by": "person"`. |

## If something goes wrong

* A page engine does not answer: the batch finishes anyway. The batch card shows an amber "A reading engine did not answer" line with the
  engine name and number of pages; the other engine and the standard reader read those pages; fewer values are auto. Say: "A model can be down and the line keeps moving."
  Tested live with each engine pointed at a dead port, and with both (`docs/run-report.md`).
* The tunnel to the GPU is down (`scripts/gpu_tunnel.sh` restarts itself): both engines fail, pages go down the standard path, nothing
  is auto beyond spot check. Restart with `scripts/run_local_stack.sh stop` then `start 3`.
* A document stays "processing" for minutes: it is a multi-page file. Wait, or upload single pages.

## What to say, and what not to say

* Say: the numbers are from 8 real documents the team labelled quickly, on the development set; the final test has not been spent.
* Do not quote accuracy or % auto to the room as a result: labels are unverified drafts and the set has no phone photos.
* Do say what is true today: on those 8 documents 228 of 264 fields were right, 153 were approved without a person, and none of
  those 153 was wrong in the live run (`docs/reader-results.md`, "Default configuration").
