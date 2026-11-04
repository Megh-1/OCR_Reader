# IDP Pipeline: Architecture

> **What this document is.** The architecture of the IDP Pipeline repository as it exists on disk today, written from the code, `docker-compose.yml`, `app/config.py` and the Alembic migrations. Where something is *not* built (authentication, metrics, a cache), it says so instead of describing an aspiration. Measured numbers come from the comments in `app/config.py` and the project's `CLAUDE.md`; anything that is a proposal is labelled **Proposed**.
>
> **Last updated 2026-11-04**, after commit `6877a9a`. This revision replaces the earlier "Tesseract and the PDF text layer only" description: the vision-model reader is now part of the application and the model that reads is a configuration choice (§2.9). Work that is in the working tree and not committed is marked 🟡.
>
> **Diagrams** are [Mermaid](https://mermaid.js.org/). They render on GitHub, GitLab, VS Code (Markdown preview with the Mermaid extension), and most Markdown viewers.

**Status legend used throughout**

| Tag | Meaning |
|---|---|
| ✅ **Built** | In the code and committed, covered by the test suite (596 tests collected; 595 passed at the last full run, and the one that failed, a replay test, passed when run on its own afterwards while engine files were still being edited) |
| 🟡 **Uncommitted** | Built and tested, but sitting in the working tree, not yet in a commit |
| 🧪 **Research tooling** | Training and measurement scripts (`scripts/gpu/*`, `make_train_set.py`, `score_*.py`); **not part of the running application** |
| ⛔ **Not built** | Does not exist yet |

---

## Table of contents

1. [System overview & high-level design](#1-system-overview--high-level-design)
2. [Core components & responsibilities](#2-core-components--responsibilities)
3. [Data architecture & schema strategy](#3-data-architecture--schema-strategy)
4. [API & communication contracts](#4-api--communication-contracts)
5. [Security, auth & environment](#5-security-auth--environment)
6. [Non-functional requirements & scale](#6-non-functional-requirements--scale)
7. [Architecture decision records](#7-architecture-decision-records-adrs--trade-offs)
8. [How the technology works right now: status and the vision reader](#8-how-the-technology-works-right-now)
9. [Appendix: repository map, run commands, glossary](#9-appendix)
10. [Research survey and recommended direction](#10-research-survey-and-recommended-direction)

---

## 1. System overview & high-level design

### 1.1 What the system does

The IDP Pipeline turns piles of scanned or digital documents into structured JSON. A person uploads a sample page of a document type once and **marks it up in the browser**: they draw *anchor* boxes around printed labels that identify the form, and *field* boxes around the values to extract. That markup is saved as a versioned **template**. After that, a user uploads a batch of documents (PDF, JPEG or PNG). Each page is cleaned up, matched to a template by its anchors, and read field by field: straight from the PDF's embedded text when it has one, by a **vision model** that reads template-guided crops of scanned and photographed pages (served by a separate GPU machine, and swappable by configuration), and by Tesseract OCR when the model is not available. Every value is then checked against rules the template author wrote (a regex, a check digit, arithmetic between fields) and given an **approval tier**: `auto` (safe to use without a human), `spot_check` (probably right, show it to a person), or `review` (rejected, a person must decide). Anything that is not `auto` lands in a **review queue** where a person confirms or corrects it next to a crop of the source page. The design principle that governs every choice is taken from the project's spec: **refuse and flag rather than emit a wrong value.**

### 1.2 Design philosophy and paradigm

| Aspect | Choice |
|---|---|
| **Paradigm** | **Modular monolith, two process types.** One Python codebase (`app/`) runs as an API server (FastAPI) and as N batch workers (RQ). Same code, same image, different entry command. |
| **Extraction approach** | **Template-driven; the model reads, it does not decide where.** A person declares where the values are; nothing is inferred. Three readers share one validation path: the PDF text layer (exact), a vision model reading crops that the template and page alignment cut (opt-in per template, off unless a reader engine is configured), and Tesseract (the fallback, and the whole path when no engine is set). Whatever reads, Module 7 validates and the tiers follow. |
| **Which model reads** | **Configuration.** A named *reader engine* in a JSON registry: our own service (`/read`) or any OpenAI-compatible server (vLLM, SGLang, Ollama, a hosted model). Changing the model is editing that file and restarting, not changing code (§2.9). |
| **Failure philosophy** | Every uncertain outcome becomes a *queue entry for a human*, never a guess. A check that cannot decide is `skipped`, never `passed`. Every safeguard only ever adds caution. |
| **State** | PostgreSQL is the system of record for everything durable (templates, results, review queue, batch state, even the uploaded bytes while a document is in flight). Redis holds only the job queue. |
| **Frontend** | A single-page console (React) served as static files and talking to the API through a same-origin reverse proxy. |
| **Templates** | **Append-only**: a save always inserts a new version, never edits one. A past extraction stays reproducible against the exact version that ran. |

### 1.3 System context

```mermaid
flowchart LR
    user(["👤 Operator<br/>uploads, reviews,<br/>marks up templates"])
    sys{{"<b>IDP Pipeline</b><br/>template-driven<br/>document extraction"}}
    consumer(["⚙️ Downstream system<br/>consumes JSON via<br/>GET /results or the batch export"])
    docs[("📄 Documents<br/>PDF / JPEG / PNG<br/>≤ 15 MB, ≤ 20 pages")]
    gpu[["🖥 Reader engine (optional)<br/>vision model on a GPU machine,<br/>crops in, text out"]]

    user -- "marks templates,<br/>uploads batches,<br/>resolves flagged fields" --> sys
    docs -- "uploaded" --> sys
    sys -- "extracted fields, tier, confidence,<br/>who verified it · or the strict<br/>batch export" --> consumer
    sys -- "review & spot-check<br/>queue" --> user
    sys -. "field crops<br/>(scans and photos)" .-> gpu
    gpu -. "one reply per crop" .-> sys

    classDef sysbox fill:#1f4fd8,stroke:#0b2a80,color:#fff
    class sys sysbox
```

---

## 2. Core components & responsibilities

### 2.1 Deployment topology (Docker Compose)

`docker compose up -d --build` brings up five long-running containers (plus a `tests` container behind a profile). Verified on Docker Engine 29 + Compose 2.40 inside WSL2.

```mermaid
flowchart TB
    browser(["🌐 Browser"])

    subgraph host["Docker host  (docker-compose.yml)"]
        direction TB
        fe["<b>frontend</b><br/>nginx :80 → host :8080<br/>static React build<br/>+ reverse proxy"]
        app["<b>app</b><br/>uvicorn + FastAPI :8000<br/>runs <code>alembic upgrade head</code> on start<br/>image: idp-pipeline:runtime"]
        wk["<b>worker</b> × N<br/>python -m app.worker<br/>RQ worker + scheduler<br/>same image as app"]
        pg[("<b>postgres:17</b><br/>host :5433<br/>db idp_pipeline<br/>volume postgres_data")]
        rd[("<b>redis:7-alpine</b><br/>host :6380<br/>RQ queue 'idp-batch'")]
        tess[["Tesseract 5.5 binary<br/>(inside app & worker image)"]]
    end
    gpu[["<b>Reader engine</b> (not in compose)<br/>serve_reader.py on a GPU machine,<br/>or any OpenAI-compatible server"]]

    browser -->|"HTTP :8080"| fe
    fe -->|"/templates /review /results /documents<br/>proxy_pass → app:8000"| app
    app -->|"SQLAlchemy + psycopg"| pg
    app -->|"enqueue job"| rd
    wk -->|"dequeue job"| rd
    wk -->|"SQLAlchemy + psycopg"| pg
    app -.->|"subprocess (sync endpoint)"| tess
    wk -.->|"subprocess × 8 threads"| tess
    wk -.->|"HTTPS: ~87 crops per scanned page<br/>bearer token"| gpu
    app -.->|"same, for the sync endpoint"| gpu

    classDef store fill:#eef3ff,stroke:#1f4fd8
    class pg,rd store
```

Key facts that shape this topology:

- **The API and the worker share nothing but Postgres and Redis.** Uploaded bytes are stored *on the database row*, not on a shared disk, so any worker on any machine can process any document.
- **The app container migrates the database on start** (`CMD ["sh","-c","python -m alembic upgrade head && uvicorn ..."]`); the worker waits for the app's health check for that reason.
- **Every field read is its own Tesseract process**, so the number of OS processes per document is large; see §6 for the OpenMP cap that keeps that from thrashing.
- **The reader engine is not part of the compose stack.** A 7B vision model needs a GPU the laptop does not have, so it runs elsewhere (a rented or shared GPU machine) and the app is its client. It is **off** until `VLM_READER_URL` (or a registry file) is set; when it is down or unreachable the page is read with Tesseract and says so (`reader: "ocr_fallback"`). The crops are pieces of business documents and leave the host: use a token, a private port and TLS.
- **Two reverse-proxy copies must stay in sync.** `frontend/vite.config.js` (dev) and `frontend/nginx.conf` (container) both list the route prefixes the UI calls. A new backend prefix needs an entry in *both*.

### 2.2 Backend modules (numbered after the spec)

The nine modules from the reference PDF are plain Python functions that call each other's *public* functions; none re-implements another.

```mermaid
flowchart LR
    subgraph API["FastAPI app  (app/main.py: 9 routers)"]
        M1["<b>1 Ingestion</b><br/>ingestion.py<br/>MIME sniff, size & page caps,<br/>PDF → 200 DPI pages,<br/>text-layer extraction"]
        M2["<b>2 Preprocessing</b><br/>preprocessing.py<br/>page detect, deskew,<br/>CLAHE, binarize, rule removal"]
        M5["<b>5 Type identification</b><br/>type_identification.py<br/>anchor OCR vs every template"]
        AL["<b>Alignment</b><br/>alignment.py<br/>landmark words → shift+stretch"]
        M6["<b>6 Field extraction</b><br/>field_extraction.py<br/>text-layer OR Tesseract"]
        VR["<b>Vision reader</b><br/>vlm_reader/<br/>crops → engine → staged answer<br/>→ gate → fixed confidence"]
        EN["<b>Reader engines</b><br/>engines.py<br/>named registry: service |<br/>openai_compatible"]
        M7["<b>7 Validation</b><br/>validation.py + checks.py<br/>regex · checksum · confidence<br/>· cross-field checks → tier"]
        M8["<b>8 Output compiler</b><br/>output_compiler.py<br/>JSON result, persists,<br/>owns /results*"]
        M9["<b>9 Review queue</b><br/>review_queue.py<br/>field_review · spot_check<br/>· new_template_needed"]
        RG["<b>Review groups</b><br/>review_groups.py<br/>by field · by value · by document<br/>bulk accept · correct a group<br/>accept agreed · audit sample · evidence"]
        TR["<b>Trust</b><br/>trust.py<br/>audited auto-approval"]
        EX["<b>Export</b><br/>export.py<br/>strict batch JSON,<br/>who verified each value"]
        HK["<b>Housekeeping</b><br/>housekeeping.py<br/>clear queue · clear history"]
        M34["<b>3+4 Template store</b><br/>template_store.py<br/>append-only versions, reads"]
        PIPE["<b>pipeline.py</b><br/>the wiring + POST /documents/process"]
        BATCH["<b>batch.py</b><br/>queue wrapper around the same pipeline"]
    end

    PIPE --> M1 --> M2 --> M5 --> M6 --> M7 --> M8
    M5 -. "no match / ambiguous" .-> M9
    M7 -. "flagged / spot_check fields" .-> M9
    M9 --- RG
    RG --> TR
    TR -. "trusted fields skip the queue" .-> PIPE
    M5 --> AL
    AL --> M6
    AL --> VR
    VR --> EN
    VR -. "reader unavailable → Tesseract" .-> M6
    BATCH -.-> EX
    M9 --- HK
    M5 --- M34
    M6 --- M34
    M7 --- M34
    BATCH --> PIPE
```

| Module | File | Responsibility | Notable rule that must hold |
|---|---|---|---|
| 1 Ingestion | `ingestion.py` | Validate type (libmagic, not the extension), size (15 MB), page count (20); render PDF pages at 200 DPI; pull the embedded text layer | Invisible text (render mode 3, how OCR software makes a scan searchable) is **not** native text; coordinates go through `page.rotation_matrix` |
| 2 Preprocessing | `preprocessing.py` | Page-quad detection + four-point warp, deskew (contour → Hough → OSD last), CLAHE, denoise, binarize, optional ruled-line removal | Produces **two renderings with identical dimensions**: binarized (anchors only) and grayscale (fields + review). Never feed one to both |
| 5 Type ID | `type_identification.py` | OCR each template's anchor crops, fuzzy-match (rapidfuzz), require **all** anchors to pass (or `min_anchors_matching` of them, never fewer than 2) | More than one matching template is `ambiguous_match`, never a pick, **except** when one template's anchors contain all the others' (§2.8) |
| Alignment | `alignment.py` | OCR the whole page once, match the template's landmark words, RANSAC-fit scale+shift per axis, add local median offset | Returns `None` below 12 agreeing words. The boxes that *verify* a page are the boxes that *read* it |
| 6 Extraction | `field_extraction.py` | Per field: read PDF words inside the box, or Tesseract at PSM 4/6/7 keeping the most confident non-empty read | Only edge-trimming of rule fragments is allowed; content is never "corrected" |
| 7 Validation | `validation.py`, `checks.py` | Regex, check digit (IATA AWB mod-7), confidence floor, cross-field checks; assigns tier | A field with no regex and no checksum is **never `auto`**, however confident |
| 8 Output | `output_compiler.py` | Compile accepted + flagged fields into `CompiledResult`, store a JPEG of the page for review | Flagged field → `overall_status = needs_review`; accepted fields are never dropped |
| 9 Review queue | `review_queue.py` | Three entry types; resolving a field clears `needs_review` but keeps the machine's own tier | Tiers only ever *add* caution |
| Vision reader | `vlm_reader/` (`reader.py`, `crops.py`, `decide.py`, `words.py`, `waybill.py`, `profile.py`) | For a scanned or photographed page of a template that opts in (`hints.vlm_reader`): whole-page OCR, landmark alignment, a window per field at three widenings, one question per crop to the engine, staged answers, compared with a word reader on the page OCR | The model reports no confidence, so values get **fixed** ones (gated 89, ungated 70), which is under the auto threshold on purpose: **nothing the model reads reaches `auto`** |
| Reader engines | `vlm_reader/engines.py`, `client.py`, `openai_client.py` | Resolve the active engine by name from `READER_ENGINES_FILE` (else `VLM_READER_URL`); two kinds, one `read(items) -> replies` interface | Any failure is `ReaderUnavailable`, never a crash or a half-read page; tokens come from an env var named in the file, never the file |
| Review groups | `review_groups.py` | Group pending entries **by field** (one field across the upload's documents), by value, or by document; bulk accept; **correct a whole group** with one typed value; **accept agreed** model reads; the evidence report | The **server** decides what may be bulk-accepted: only a spot check with a value, never a Module 7 rejection; every bulk accept or group correction holds back a random audit sample; review is scoped to one batch |
| Export | `export.py` | `GET /documents/batch/{id}/export`: per field value, status (`confirmed` / `accepted` / `auto` / `unverified`), confidence | **Strict by default**: an unverified value is `null` with the machine's reading under `suggested`; nothing is emitted that nobody checked and no rule vouched for |
| Housekeeping | `housekeeping.py` | Clear the pending queue (all, one batch, or one field) and the processed history | Confirmed deletes only; resolved entries and the results they point at are **kept** (they are the trust evidence); history is refused while a document is in flight |
| Trust | `trust.py`, `utils/stats.py` | Per (template, **version**, field): spot checks a person resolved one at a time, how many they changed, the exact Clopper-Pearson upper bound; a field under the target skips the spot-check queue | Bulk accepts are not evidence; one wrong value found raises the bound and withdraws the trust |
| 3+4 Templates | `template_store.py` | Write/validate/persist and read queries | `create_template_version` always INSERTs; no update/delete path exists |
| Batch | `batch.py` | `POST /documents/batch`, status, per-document retry/resume | See §4.4 |

### 2.3 The per-page pipeline (the core of the system)

This is what `pipeline.process_page` does for each page, whether called by the synchronous endpoint or by a batch worker.

```mermaid
flowchart TD
    A(["Page bytes + optional type_hint"]) --> B{"Has real embedded<br/>text layer?"}
    B -- "yes (text-native PDF)" --> C1["Pass 1: preprocess,<br/><b>skip deskew</b>"]
    B -- "no (scan / photo)" --> C2["Pass 1: preprocess generically<br/>page detect → deskew → CLAHE → binarize"]
    C1 --> D
    C2 --> D["<b>Module 5</b> identify type<br/>OCR anchor crops on the<br/><i>binarized</i> rendering,<br/>AND across all anchors"]
    D --> E{"confirmed?"}
    E -- "no_match / ambiguous_match /<br/>unknown_type_hint" --> Q1[/"Review queue:<br/><b>new_template_needed</b>"/]
    Q1 --> Z1(["PageResult confirmed=false"])
    E -- yes --> F["get_template_version(<b>exact</b> id, version)<br/>never 'latest'"]
    F --> G{"text layer?"}
    G -- yes --> H1["Module 6a: words whose box is ≥50%<br/>inside the field box, reading order,<br/>confidence fixed at 100"]
    H1 --> H1V{"Module 7 rejects anything<br/>AND template opts in<br/>AND an engine is set up?"}
    H1V -- "no: clean page" --> I
    H1V -- "yes: the boxes probably missed" --> VR1
    G -- no --> VQ{"template has hints.vlm_reader<br/>AND an engine is set up?"}
    VQ -- yes --> VR1["<b>Vision reader</b><br/>page OCR → align → crops (3 widenings)<br/>→ engine → staged answer → gate<br/>fixed confidence 89 / 70 / 0"]
    VR1 -- "reader unavailable<br/>(down, no profile, not aligned)" --> FB["Tesseract path, reader = ocr_fallback<br/>tier capped at spot_check"]
    VQ -- no --> H2
    FB --> H2["Pass 2 preprocess with the template's hints<br/>(page_aspect_ratio, ruled_form)<br/>re-fit alignment if the frame changed"]
    H2 --> H3["Module 6b: Tesseract per field,<br/>PSM 4/6/7 (+ digit whitelist),<br/>8 fields in parallel,<br/>on the <i>grayscale</i> rendering"]
    VR1 -- ok --> I
    H3 --> I["Field options:<br/>from_field · extract_pattern ·<br/>squash_spaces · optional"]
    I --> J["<b>Module 7</b> validate<br/>regex · checksum · confidence ·<br/>cross-field checks"]
    J --> K["<b>Module 8</b> compile + persist<br/>extraction_results (+ page JPEG)"]
    K --> L{"per field tier"}
    L -- "review (rejected)" --> Q2[/"field_review"/]
    L -- "spot_check (accepted, doubtful)" --> TQ{"field trusted?<br/>(trust.py)"}
    TQ -- "no" --> Q3[/"spot_check<br/>why: below_auto_threshold |<br/>no_validator | blank_optional"/]
    TQ -- "yes, 95% of values" --> OK2(["🟢 auto<br/>auto_basis = trusted_field"])
    TQ -- "yes, random 5%" --> Q4[/"spot_check with audit = true"/]
    L -- "auto" --> OK(["✔ no human needed"])
    Q2 --> Z2(["PageResult confirmed=true"])
    Q3 --> Z2
    Q4 --> Z2
    OK --> Z2
    OK2 --> Z2
```

**Which reader a page gets** (`pipeline.extract_page_fields`, shared with the accuracy harness so the two cannot drift):

| Page | Reader | `PageResult.reader` |
|---|---|---|
| PDF with a real text layer, Module 7 accepts it | the layer's words | `text_layer` |
| PDF with a text layer that Module 7 **rejects anything on** (the layer sat off its cells: another print, a form fed through the printer off-centre) | the vision reader, with the layer's exact words as the second reader; if the engine is down, keep the layer | `vlm` / `text_layer` |
| Scan or photo, template opts in, engine up | the vision reader | `vlm` |
| Scan or photo, template opts in, engine **down** or page not alignable | Tesseract, **capped at `spot_check`** | `ocr_fallback` |
| Template does not opt in, or no engine configured | Tesseract | `ocr` |

The cap on `ocr_fallback` is a measured rule: on 50 hard waybills, 4 of the 13 values that reached `auto` were wrong at confidence 93–96, all on camera photos read by the fallback (`1788.00` for `391.00`).

**Two-pass preprocessing is the central design.** Module 2 runs *before* the document type is known, so it preprocesses once generically (enough to find and read anchors), then again with the confirmed template's `hints` (the page's true aspect ratio, whether it is a ruled form). A template with no hints costs no second pass.

### 2.4 Approval tiers: how a value is judged

```mermaid
flowchart LR
    V["Extracted value<br/>+ confidence"] --> R{"Module 7:<br/>regex ✔ AND<br/>checksum ✔ AND<br/>confidence ≥ floor<br/>AND cross-checks ✔?"}
    R -- "any fail" --> REV["🔴 <b>review</b><br/>rejected, reasons listed,<br/>person must decide"]
    R -- "all pass" --> HV{"Field has a regex<br/>or a checksum?"}
    HV -- "no" --> SPOT1["🟡 <b>spot_check</b><br/>why = no_validator"]
    HV -- "yes" --> CT{"confidence ≥ 0.90?"}
    CT -- "no" --> SPOT2["🟡 <b>spot_check</b><br/>why = below_auto_threshold"]
    CT -- "yes" --> AUTO["🟢 <b>auto</b><br/>used without a human"]
```

**Audited auto-approval (§2.7)** adds one more way to reach `auto`: a `spot_check` on a field a person has checked enough of is approved as `auto` with `auto_basis = trusted_field`, bar a random 5% that still go to a person. It only ever applies to `below_auto_threshold` and `no_validator` spot checks that have a value; a Module 7 rejection or a blank optional is never promoted.

**Values the vision model read never reach `auto`.** The model reports no confidence, so each value is given a fixed one by what was established about it: **gated** (a second reader agrees, or an arithmetic fact of the document holds, **and** the format passes) 89, anything else 70, a blank 0 (70 when the field is optional). 89 is under the 90 auto threshold on purpose: the gate measured 0 wrong of 236 auto-eligible fields on real pages but 2 wrong of 955 arithmetic-gated synthetic fields (0.2%), which is not 1 in 10,000. Raising it to 90 is a decision someone makes with eyes open, not a default. Audited auto-approval is also never applied to a vision-read page: the evidence it counts was gathered on another reader.

**Who verified a value** is recorded: `FieldResult.verified_by` is `person` (resolved one at a time, or corrected on Results) or `bulk` (accepted from a group). The strict export (§4.3) uses it, and the trust evidence counts only the `person` ones.

Why the "no validator ⇒ never auto" rule exists: on the synthetic corpus **every** wrong value Module 7 ever accepted sat in a field with no regex or checksum, at confidence 64–87, only 3–5 points below the 0.90 threshold. The rule closes that gap at a measured cost of 186 correct reads held back as spot-checks.

### 2.5 Frontend architecture

React 19 + Vite 8 + Tailwind v4, shadcn-style components, Fabric.js for the template canvas. There is **no frontend test runner**; the build is checked by `oxlint` and by looking at it in a browser.

```mermaid
flowchart TB
    subgraph SPA["frontend/src  (hash-routed single-page app)"]
        APP["<b>App.jsx</b><br/>nav · review-queue badge ·<br/>tier legend · hash router"]
        subgraph screens["screens/"]
            S1["<b>BatchMonitor</b><br/>#/batch (default)<br/>upload + 1 s status polling,<br/>flow stepper, Download JSON,<br/>clear history"]
            S2["<b>Results</b><br/>#/results<br/>table, in-place correction,<br/>JSON download"]
            S3["<b>ReviewQueue</b><br/>#/review (this upload)<br/>By field · Same value ·<br/>By document · Evidence<br/>Accept agreed · clear queue"]
            S4["<b>TemplateDesigner</b><br/>#/templates<br/>Fabric.js canvas:<br/>anchors, fields, checks"]
        end
        subgraph shared["shared"]
            API["<b>api.js</b><br/>fetch wrapper,<br/>ApiValidationError,<br/>error-line formatter"]
            COMP["components/<br/>FieldColumn · GroupedReview · FieldCrop ·<br/>ConfirmDialog · ConfidenceGauge ·<br/>TierBadge · StatusPill · ui/*"]
            LIB["lib/tiers.js · nav.js<br/>geometry.js · useDocumentCanvas.js"]
            CSS["index.css<br/>one design-token source<br/>(contrast-checked)"]
        end
    end

    APP --> S1 & S2 & S3 & S4
    S1 & S2 & S3 & S4 --> API
    S1 & S2 & S3 & S4 --> COMP
    S3 & S4 --> LIB
    API -->|"same-origin fetch"| PROXY["nginx / vite proxy"]
```

| Screen | API calls it makes (from `api.js`) |
|---|---|
| Upload (`BatchMonitor`) | `submitBatch`, `getBatchStatus`, `listBatches`, `listTemplates`, `clearHistory`; the **Download JSON** link is `GET /documents/batch/{id}/export` |
| Results | `listResults`, `getExtractionResult`, `correctResultField`, `getTemplateVersion` |
| Review | `getReviewGroups` (by field / value / document), `bulkAccept`, `bulkCorrect`, `getAgreed`, `acceptAgreed`, `clearReviewQueue`, `getReviewEvidence`, `listReviewQueue`, `resolveFieldReview`, `resolveNewTemplateNeeded`, `getExtractionResult`, `getTemplateVersion`, `resultImageUrl` |
| Templates | `createTemplate`, `getTemplate`, `listTemplates`, `listTemplateVersions`, `getTemplateVersion`, `listResults`, `resultImageUrl` |

**The Review screen is scoped to one upload** (the newest by default; `#/review/<id>` for another, `#/review/all` for everything) and opens on **By field**: one field across all the upload's documents, the same crop repeated down a column, so a person scans one kind of text in one place, fixes the wrong values in place, and accepts the rest in one click (a random 10% stays for a proper look). Per column there is also **Leave blank** for a field nobody needs (its pending entries are removed; the JSON leaves it empty). **Same value** and **By document** are the other views. A bar at the top offers **Accept agreed values** (§2.7).

Design rules enforced in `index.css`: 4-pt spacing steps, IBM Plex Sans, **one** brand color for everything interactive, and green/amber/red used *only* as data states (auto / spot check / review), always paired with an icon and a label so color is never the only signal. `node frontend/scripts/check_contrast.mjs` verifies every color pair.

### 2.6 End-to-end data flow: a batch upload

```mermaid
sequenceDiagram
    autonumber
    actor U as Operator (browser)
    participant N as nginx :8080
    participant A as FastAPI app
    participant P as Postgres
    participant R as Redis (RQ)
    participant W as Worker
    participant T as Tesseract
    participant G as Reader engine (GPU)

    U->>N: POST /documents/batch (multipart files, type_hint?)
    N->>A: proxy (client_max_body_size 800m)
    A->>A: validate each file (MIME, ≤15 MB), ≤50 files
    A->>P: INSERT batches, flush, then INSERT batch_documents (status=queued, payload=bytes)
    A->>R: enqueue one job per document (Retry max=3, 10s/60s/300s)
    A-->>U: 202 { batch_id, status_url }

    loop every 1 s (until terminal)
        U->>A: GET /documents/batch/{id}/status
        A->>P: SELECT batch + documents
        A-->>U: per-doc status, attempts, pages_done, result when completed
    end

    R-->>W: next job
    W->>P: status=processing, attempts+=1
    loop for each unfinished page
        alt scan or photo, template opts in, engine up
            W->>T: whole-page OCR (alignment, word reader)
            W->>G: ~87 crops (HTTPS, bearer token)
            G-->>W: one reply per crop
        else text layer, or engine down or page not alignable
            W->>T: read fields (subprocess × 8)
        end
        W->>P: INSERT extraction_results, review_queue rows
        W->>P: save progress to batch_documents.result
    end
    W->>P: status=completed, payload=NULL (upload dropped)

    U->>A: GET /review/groups?by=field&batch_id=…, POST /review/bulk, PATCH /review/{id}
    A->>P: resolve entries (via = single | bulk), clear needs_review, record verified_by
    U->>A: GET /documents/batch/{id}/export
    A-->>U: strict JSON: unverified values are null, with the machine's reading under suggested
```

### 2.7 Keeping the review queue short without losing accuracy

The queue used to hold one row per doubtful field: 2,000 documents with even 10 doubtful fields each is 20,000 rows, most of them correct. Three mechanisms, each built so that it **adds measurement** rather than removing it.

```mermaid
flowchart TB
    Q["Pending queue<br/>field_review · spot_check"] --> G["<b>Grouped view</b><br/>same value across documents<br/>or all fields of one document"]
    G --> B{"Bulk accept?<br/><b>server decides</b>"}
    B -- "field_review (Module 7 rejected it)<br/>blank_optional · empty value<br/>already resolved · audit sample" --> X["refused:<br/>whole request returns 422<br/>with a reason per entry"]
    B -- "spot_check, value present" --> H["accept all but a<br/><b>random audit sample</b><br/>(10%, never under 5%, at least 1)"]
    H --> R1["resolved, via = bulk<br/><i>not counted as evidence</i>"]
    H --> R2["held back: audit = true,<br/>a person checks them one by one"]
    R2 --> E["<b>Evidence</b><br/>checked / changed per<br/>(template, version, field)"]
    P["person resolves<br/>a spot check one at a time"] --> E
    C["person corrects a<br/>trusted-auto value on Results"] --> E
    E --> U{"Clopper-Pearson upper bound<br/>≤ 1 in 10,000 at 95%?<br/>(29,956 clean checks)"}
    U -- "yes" --> T["<b>Trusted</b>: that field's spot checks<br/>become auto (95%) or audit (5%)"]
    U -- "no, or one error found" --> N["spot checks go to the queue"]
    T -. "an audit finds a wrong value" .-> N
```

```mermaid
sequenceDiagram
    autonumber
    actor Rev as Reviewer
    participant UI as Review > Grouped
    participant API as /review/*
    participant DB as review_queue + extraction_results
    participant PIPE as pipeline.process_page
    Rev->>UI: open "Same value": USD × 15 in Currency
    UI->>API: GET /review/groups?by=value
    API-->>UI: groups (entry_ids, result_ids, template version)
    Rev->>UI: scans the crop sheet, Accept all 15
    UI->>API: POST /review/bulk {entry_ids}
    API->>DB: validate EVERY entry (all-or-nothing)
    API->>DB: resolve 13 (via = bulk), mark 2 audit = true
    API-->>UI: accepted 13, held_for_audit 2
    Rev->>UI: checks the 2 audit samples one by one
    UI->>API: PATCH /review/{id}
    API->>DB: resolve (via = single) = evidence
    Note over PIPE,DB: later, a new page of the same template version
    PIPE->>DB: trusted_fields(template, version)  (30 s cache)
    alt field trusted and not chosen for audit
        PIPE->>DB: store tier = auto, auto_basis = trusted_field, no queue row
    else trusted but chosen for audit
        PIPE->>DB: enqueue spot_check, audit = true
    else not trusted
        PIPE->>DB: enqueue spot_check
    end
```

| Rule | Why |
|---|---|
| Bulk accept only for `spot_check` with `why` ∈ {`below_auto_threshold`, `no_validator`} and a non-empty value | A `field_review` failed Module 7; accepting it unseen would override the refusal principle. A blank optional needs a look at the page to confirm the value is really absent |
| One refused entry refuses the whole request | No half-applied bulk accept |
| Every bulk accept keeps ≥ 5% (default 10%, at least one when ≥ 2) for a person | What was accepted unseen is still **measured**; audit samples can never be bulk-accepted later |
| Evidence counts only `via = single` spot checks | Accepting a crop sheet is not an independent look at each value |
| Evidence is per template **version** and per field | A new version has new boxes; old checks say nothing about it |
| Trust needs the exact one-sided bound ≤ target | With no checks the bound is 1; 0 errors in *n* gives ≈ 3/*n* (rule of three); 1 error in 20 gives 0.216 at 95% |
| A trusted-auto value later corrected on the Results screen writes a resolved spot-check row | That is precisely the error the trust must hear about; it has no queue row of its own |
| **Correct a group** (`POST /review/bulk-correct`): one typed value to every entry of a value group (2 or more, one field of one template version) | The value must **fullmatch the field's regex**, as Module 7 would; 10% are held back as audit samples; it resolves `via = bulk`, so it is never evidence. It may take a value Module 7 rejected, which is why it is the one bulk path that needs a typed value |
| **Accept agreed** (`GET /review/agreed`, `POST /review/accept-agreed`): the server picks spot checks the vision model read and an independent check backed (gated, confidence 89) | A shortcut, not auto-approval: **4 of 797 such values were wrong (0.5%, 95% upper bound ≈ 1.3%)** on the 50-document batch, nowhere near 1 in 10,000, and the confirmation dialog says so. Same audit hold as any bulk accept |
| **Leave blank** (`POST /review/clear` with `field_id`) | Removes that field's pending entries in the batch; the value stays in the result as the machine's reading and the strict export leaves it empty |

#### What the queue looks like on a large batch (measured)

The 50 hard documents of §6.2 (1,518 fields on the 46 documents that were identified). With the vision model reading, **nothing reached `auto`**, so every value needed either a person or one of the shortcuts above:

| | Count |
|---|---|
| Fields that stay in the queue (not gated, or Module 7 rejected) | **≈ 670** of 1,518 |
| Model values read **and independently backed** ("agreed"), acceptable in one click with an audit hold | **771** (latest batch) |
| Wrong values among the agreed ones | 4 of 797 (0.5%) |
| Wrong values the spot checks must catch (model reads, not gated) | 58 in `spot_check` + 59 in `review` |

**A target of "20–30 review items per 50 documents" is not reachable at an error rate near 1 in 10,000 with this model and these documents**, and the queue was not made to look shorter by lowering the bar. Two things were tried and measured before concluding that:

- **Tesseract as a second reader** agrees with the model on only **55 of 624** uncertain values and is right on **1 of the 117** model errors, so its agreement says almost nothing: it is not a gate (§8.4).
- **Grouping** helps the *person*, not the count: 224 queue entries sat in 56 repeated-value groups, 376 were singletons and 73 were blanks; only 10 of the 113 wrong values in the queue sat in a repeated group.

What shortens the work in practice is the review **shape** (one field across all documents, fix and accept), Accept agreed, correcting a group once, and Leave blank. A second reader that is itself strong is the one lever left (§2.9, §10.3): two different models agreeing is worth more than a model agreeing with Tesseract, and that is measurable with `scripts/evaluate_reader.py`.

**What this does and does not do.** It cannot certify 1 error in 10,000 from a few hundred documents: that needs **29,956 checked values with none wrong, per field** (so a fresh install trusts nothing). It makes the evidence accrue as a by-product of normal review, and it gives the queue a short path (grouping, bulk) in the meantime. The 5% audit rate and the 30 s cache are starting figures, not measurements.

### 2.8 Telling templates apart: whole forms, parts of forms, and anchor subsets

```mermaid
flowchart TD
    UP["Page, no type hint"] --> SC["Scan every <b>unarchived, non-hint-only</b> template:<br/>OCR its anchor crops"]
    SC --> M{"how many pass?"}
    M -- "0" --> NT["new_template_needed"]
    M -- "1" --> OK["that template"]
    M -- "2+" --> MS{"does one template's anchors<br/><b>contain</b> all the others'?<br/>(same text, same place ±0.02,<br/>strictly more anchors)"}
    MS -- "yes" --> WIN["the most specific wins:<br/>a whole form beats a part cut from it"]
    MS -- "no (unrelated or equal sets)" --> AMB["ambiguous_match, never a pick"]
```

- **A part is its own template**, drawn on a sample of the part (or created in one click from selected fields of an existing template: *New template from some of these fields*, which copies the chosen fields, optionally the chosen anchors, the checks whose fields were all kept, and the nearby landmarks). It never refers back to the whole-form template; the relationship is derived at match time from anchor containment, not stored.
- **`hints.is_part`** marks a part. Left to automatic detection it needs **at least two anchors** (one label would match too many pages); with `hint_only` it can be chosen by hand with one.
- **`hints.yields_to`**: a template that names another stands down **only when that other also passed its own anchors on this page**. `air_waybill` yields to `air_waybill_bkk` because its IATA-boilerplate anchors pass every Bangkok-form scan; after the rule 9 of 9 Bangkok-form files resolve to `air_waybill_bkk` (before, each was `ambiguous_match` and the model never ran). A type hint ignores it; two templates that yield to each other stay ambiguous.
- **`hints.min_anchors_matching`**: require N of M anchors instead of all, with 2 ≤ N ≤ M. It lets one smudged or cut-off label not refuse a page. It never lowers the bar below two matching labels, and field boxes that were drawn on a whole page still sit where they sat on the whole page, so a half-page scan needs boxes and anchors drawn on a half-page sample.
- **The batch status carries a "where the pages went" summary** (`summary.by_template`, `summary.unmatched`), so a batch that spread over several templates, or lost pages to `no_match`, says so.
- **The rule editor** (Templates > Checks): lists a template's cross-field rules with a *Test* button (dry run over already-processed documents), proposes rules that held on every readable document (`suggest_checks`, at least two documents, non-degenerate), and writes new `product`/`sum`/`equal`/`one_of` rules. A suggestion is only a proposal until a person presses Add.

### 2.9 Reader engines: changing the model is configuration

The question "which model reads the crops" is separated from "how a form is read". A template says **how** (`hints.vlm_reader` names a *profile* of the form's printed rules, zones and wording); an **engine** says **who** answers.

```mermaid
flowchart LR
    T["Template<br/>hints.vlm_reader = waybill_B<br/>(the FORM profile)"] --> RP["vlm_reader.read_page<br/>crops, questions,<br/>staged answers, gate"]
    RP --> SEL{"engine chosen by<br/>READER_ENGINE → file default → first entry<br/>(no file: VLM_READER_URL → 'default')"}
    subgraph reg["Registry: READER_ENGINES_FILE (JSON)"]
        E1["qwen_waybill<br/>kind = service"]
        E2["candidate_vlm<br/>kind = openai_compatible"]
    end
    SEL --> E1
    SEL --> E2
    E1 --> S1["scripts/gpu/serve_reader.py<br/>Qwen2.5-VL-7B + our LoRA<br/>POST /read"]
    E2 --> S2["vLLM · SGLang · Ollama · hosted model<br/>POST /chat/completions with image_url"]
    S1 --> R["one reply per crop,<br/>all-or-nothing"]
    S2 --> R
    R --> RP
```

```json
{ "default": "qwen_waybill",
  "engines": [
    { "name": "qwen_waybill", "kind": "service", "url": "http://127.0.0.1:8700", "token_env": "VLM_READER_TOKEN" },
    { "name": "candidate_vlm", "kind": "openai_compatible", "url": "http://gpu-box:8000/v1",
      "model": "the-name-the-server-serves", "token_env": "CANDIDATE_VLM_TOKEN",
      "concurrency": 8, "max_tokens": 512, "extra_body": {} } ] }
```

| Rule | Why |
|---|---|
| Both kinds answer one call, `read(items, max_new) -> replies`; a short or malformed answer fails the **whole** set | A page is never half read by the model and half guessed |
| Anything unusable (no file, bad JSON, unknown kind, repeated name, a misspelt setting, no model name, server down) is `ReaderUnavailable`, which the pipeline answers by reading the page with Tesseract and saying so | A broken registry is an outage of the *better* reader, not of the application |
| `token_env` names an environment variable; the file holds no secret | The registry file can be committed or shared |
| With no file, `VLM_READER_URL` / `VLM_READER_TOKEN` make one engine called `default` | Existing deployments keep working without a change |
| The `openai_compatible` reader sends one crop per request, several at once (`concurrency`), keeps the answers in order, and strips a leading `<think>…</think>` block | That is how vLLM-style servers batch; a model that reasons first still yields just its answer |

**To try a newly published model:** serve it behind an OpenAI-compatible endpoint, add an entry, then run `scripts/evaluate_reader.py --engine tesseract --engine qwen_waybill --engine candidate_vlm` on the same labelled documents. It prints, per engine and input kind, fields right, wrong values **by approval tier** (a wrong value in `auto` would ship; in `spot_check` it costs a person's attention), and documents per minute, then the **second-reader table**: where two engines give the same value, how often is it right. Only then point `READER_ENGINE` at it.

**What a registry does not do** (so nobody expects it to):

- **The wording of the questions is still the shipped adapter's.** A different model may do better or worse with other wording; that is a measured change, not a config switch. 🟡 Per-engine prompt wording is being added in the working tree.
- **Switching engines changes who reads.** The human-check evidence behind audited auto-approval was gathered on another reader (vision-read pages never get audited auto-approval anyway), and every engine's values stay `spot_check` until measured.
- **A whole-page parser is a different shape.** Baidu's Unlimited-OCR (2026) is built for very long documents and returns text with page markers, no word positions as far as its README says (§10.2). It cannot replace the page-alignment and crop machinery; as a *crop reader* it is zero-shot (no adapter for our forms) and is only a candidate until the scorecard says otherwise.
- **Which engine read a page is logged** (`VlmPage.engine`) but **not yet stored on the result**; a bake-off over live traffic needs that.

🟡 **In the working tree, not committed: measurement-based engine selection** (`app/modules/engine_selection/`, migration `0007`, `ENGINE_*` settings, off unless `ENGINE_SELECTOR_ENABLED`). Its stated design: choose which engines read a template, and in what order, from stored **scorecards** instead of the order of the registry file; promote an engine only if it beats the incumbent on the *same* documents; take an engine out of the chain when its live, person-checked accuracy falls under its scorecard; combine two engines' answers so disagreement goes to review; and read a template with no promoted engine in cold-start mode in which nothing reaches `auto`. Every threshold there is a provisional starting value.

---

## 3. Data architecture & schema strategy

### 3.1 Primary store

**PostgreSQL 17**, accessed with SQLAlchemy 2.x (`psycopg` v3), migrated with Alembic (six committed migrations, `0001`–`0006`; 🟡 `0007`, the engine-selection tables of §2.9, is in the working tree). The strategy is **relational spine + JSONB for shape-varying documents**: ids, versions, statuses and timestamps are real columns; templates, field results, queue payloads and batch results are JSONB because their shape belongs to the template/entry type, not to the schema.

### 3.2 Entity-relationship diagram

```mermaid
erDiagram
    TEMPLATES {
        int id PK
        string doc_type "indexed; logical template_id"
        int version "UNIQUE with doc_type"
        jsonb config "anchors, fields, hints, checks, landmarks"
        date created_at
        bool is_archived "the ONLY mutable thing"
    }
    EXTRACTION_RESULTS {
        int id PK
        string template_id "= templates.doc_type (logical)"
        int template_version "exact version that ran"
        timestamptz extracted_at
        string overall_status "complete | needs_review"
        jsonb fields "field_id → value, confidence, tier, reasons, auto_basis, verified_by"
        bytea page_image "review JPEG, max 1400 px wide"
    }
    REVIEW_QUEUE {
        int id PK
        string entry_type "field_review | spot_check | new_template_needed"
        string status "pending | resolved"
        timestamptz created_at
        timestamptz resolved_at
        jsonb payload "shape depends on entry_type"
        jsonb resolution
    }
    BATCHES {
        string id PK "uuid4 hex"
        timestamptz created_at
        string type_hint
    }
    BATCH_DOCUMENTS {
        int id PK
        string batch_id FK
        int position
        string filename
        string status "queued processing retrying completed failed"
        int attempts
        text error
        bytea payload "upload bytes; NULLed when terminal"
        jsonb result "grows page by page"
        timestamptz created_at
        timestamptz started_at
        timestamptz finished_at
    }

    BATCHES ||--o{ BATCH_DOCUMENTS : "contains"
    TEMPLATES ||--o{ EXTRACTION_RESULTS : "version used (logical, no FK)"
    EXTRACTION_RESULTS ||--o{ REVIEW_QUEUE : "payload.extraction_result_id (logical, no FK)"
```

Notes on the schema:

- **Only one real foreign key exists** (`batch_documents.batch_id → batches.id`). The template↔result and result↔queue links are *logical*, carried inside values (`template_id`/`template_version` columns, `extraction_result_id` inside the JSONB payload). That keeps templates, results and queue entries independently writable; it also means referential integrity there is enforced by application code, not the database.
- The SQLAlchemy models declare **no `relationship()`**, so `submit_batch` must `flush()` the batch row before adding its documents.
- A *document type* is `templates.doc_type`; it is archived when its **latest** version is, so saving a new version un-archives it.

### 3.3 What a template contains (`TemplateIn`, stored in `templates.config`)

```mermaid
classDiagram
    class Template {
        template_id : str
        version : int
        anchors : TemplateAnchor[1..*]
        fields : TemplateField[1..*]
        landmarks : TemplateLandmark[0..*]
        checks : TemplateCheck[0..*]
        hints : TemplateHints
    }
    class TemplateAnchor {
        text : str  "printed label, never blank"
        bbox : x1 y1 x2 y2  "page fractions 0..1"
        match_threshold : 0..1
    }
    class TemplateField {
        id : str
        label : str
        bbox : x1 y1 x2 y2  "page fractions"
        data_type : str
        validation_regex : str?
        confidence_floor : 0..1
        checksum : "iata_awb_mod7"?
        from_field : str?
        extract_pattern : str?  "exactly ONE group"
        squash_spaces : bool
        optional : bool
    }
    class TemplateLandmark {
        text : str  "lower-case letters"
        x : 0..1
        y : 0..1
    }
    class TemplateCheck {
        kind : product|sum|equal|one_of|maps_to
        fields : str[]
        when : gate?
        patterns : regex?
    }
    class TemplateHints {
        page_aspect_ratio : float?
        ruled_form : bool
        hint_only : bool
        is_part : bool
        min_anchors_matching : int?
        yields_to : str[]  "template ids this one stands down for"
        vlm_reader : str?  "reader profile name; opts the form in to the vision reader"
    }
    Template "1" *-- "1..*" TemplateAnchor
    Template "1" *-- "1..*" TemplateField
    Template "1" *-- "0..*" TemplateLandmark
    Template "1" *-- "0..*" TemplateCheck
    Template "1" *-- "1" TemplateHints
```

Coordinates are **page-relative fractions**, not pixels, and are re-validated server-side (`0 ≤ x1 < x2 ≤ 1`) regardless of what the designer UI sent. `min_anchors_matching` (2 ≤ N ≤ anchor count) relaxes "all anchors" to "N of them". `is_part` marks a template drawn on part of a form; saved for automatic detection it needs two anchors. `hint_only` removes a template from the no-hint scan (otherwise a partial template over a layout another template already covers makes every un-hinted upload `ambiguous_match`). `yields_to` lets a template whose anchors are only shared boilerplate stand down for a more specific one that also matched (§2.8). `vlm_reader` names the reader **profile** (the form's printed rules, zones and wording, kept in `data/reader_profiles/`, git-ignored because it is cut from a real document); it does not name a model, which is the engine registry's job (§2.9). Changing the hint needs a new template version, which also restarts the human-check evidence counted for that version.

### 3.4 State machines

```mermaid
stateDiagram-v2
    direction LR
    state "Batch document" as BD {
        [*] --> queued
        queued --> processing: worker picks up
        processing --> completed: all pages done
        processing --> failed: IngestionError (not retried)
        processing --> retrying: other exception, attempts < 4
        retrying --> processing: after 10 s / 60 s / 300 s
        retrying --> failed: retries exhausted
        completed --> [*]
        failed --> [*]
    }
```

```mermaid
stateDiagram-v2
    direction LR
    state "Review-queue entry" as RQ {
        [*] --> pending: enqueued by pipeline
        pending --> resolved: PATCH /review/{id}
        resolved --> [*]
    }
    state "Result overall_status" as RS {
        [*] --> needs_review: ≥1 flagged field
        [*] --> complete: nothing flagged
        needs_review --> complete: last flagged field resolved
    }
```

### 3.5 Caching layers and state-management rules

| Layer | What exists | Rule |
|---|---|---|
| **Application cache** | ⛔ **None.** No Redis cache, no in-process memoization of templates. Every page re-reads its template version from Postgres. | Templates are immutable per `(doc_type, version)`, so an in-process cache would be safe if profiling ever asks for one |
| **Redis** | ✅ Job queue only (`idp-batch`, RQ). RQ keeps job state only briefly. | **Durable state lives in Postgres.** A batch's status must outlive Redis' retention |
| **Browser** | React component state; the review-queue badge is polled every 5 s; batch status every 1 s | No client-side store library, no persistence |
| **HTTP** | nginx marks hashed `/assets/` as `Cache-Control: public, immutable` (1 y); everything else is the app shell | The API sets no cache headers |
| **Binary data** | Uploaded bytes on the `batch_documents` row (dropped at terminal state); a review JPEG on every `extraction_results` row (kept) | Keeps API and worker decoupled; costs database size (see §6.3) |

**Rules of state that must stay true:** (1) templates are append-only; (2) Modules 5/6/7 must use the exact confirmed `(template_id, version)`, never "latest"; (3) a retry resumes at the first unfinished page, because a finished page already wrote results and queue entries and redoing it would duplicate them; (4) a reviewer resolving a field clears `needs_review` but never rewrites the machine's own `approval_tier`.

---

## 4. API & communication contracts

### 4.1 Communication patterns

| Pattern | Used? | Where |
|---|---|---|
| **REST over HTTP + JSON** | ✅ | All endpoints below |
| **Multipart upload** | ✅ | `/documents/process`, `/documents/batch`, `/ingest` |
| **Asynchronous job queue** (RQ over Redis) | ✅ | API → worker, per document |
| **Short polling** | ✅ | UI polls batch status every 1 s; the review badge every 5 s |
| WebSockets / SSE | ⛔ | Not used; polling is sufficient at this scale |
| GraphQL / gRPC | ⛔ | Not used |
| Webhooks / outbound callbacks | ⛔ | Downstream systems must pull `GET /results` |

### 4.2 Endpoint catalogue

FastAPI also serves interactive docs at `/docs` (used as the container health check).

| Method & path | Module | Purpose | Success | Typical errors |
|---|---|---|---|---|
| `POST /templates` | 3 | Save a new template **version** | 201 `TemplateOut` | 422 validation (bbox range, blank anchor, bad regex, check naming a missing field) |
| `GET /templates` | 4 | List known `doc_type`s | 200 `list[str]` | |
| `GET /templates/{doc_type}` | 4 | Latest version | 200 | 404 |
| `GET /templates/{doc_type}/versions` | 4 | Version summaries | 200 | 404 |
| `GET /templates/{doc_type}/versions/{n}` | 4 | One exact version | 200 | 404 |
| `POST /ingest` | 1 | Ingest only (debug) | 200 | 422 `{reason_code, message}` |
| `POST /documents/process` | pipeline | **Synchronous** whole-document run (OCR inline; nginx read timeout 300 s) | 200 `ProcessDocumentResponse` | 422 ingestion failure |
| `POST /documents/batch` | batch | Enqueue up to 50 documents | **202** `{batch_id, status_url, documents[]}` | 422 (too many, bad file), **503** (Redis unavailable) |
| `GET /documents/batch` | batch | Recent batches | 200 | |
| `GET /documents/batch/{id}/status` | batch | Per-document status + results | 200 | 404 |
| `GET /documents/batch/{id}/export?strict=true` | export | The batch as one JSON deliverable: per field value, status, confidence. **Strict (default) leaves unverified values `null`** with the machine's reading under `suggested` | 200 | 404 |
| `POST /documents/history/clear` | housekeeping | Delete batches, their documents, results and pending entries; keep resolved entries and the results they point at. Body `{"confirm": true}` | 200 counts | **409** while any document is queued/processing/retrying, 422 without `confirm` |
| `POST /templates/suggest-checks` | 3 | Propose cross-field rules that held on every readable processed document of a template | 200 `{documents, suggestions[]}` | |
| `POST /templates/test-check` | 3 | Dry-run one rule: passed / failed / skipped counts and up to 5 failures | 200 | 422 |
| `GET /results?limit=&template_id=` | 8 | Result summaries | 200 | |
| `GET /results/{id}` | 8 | One `CompiledResult` (the JSON deliverable) | 200 | 404 |
| `PATCH /results/{id}/fields/{field_id}` | 8 | Correct a value in place | 200 `FieldResult` | 404 |
| `GET /results/{id}/image` | 8 | Review JPEG of the page | 200 `image/jpeg` | 404 |
| `GET /review?status=&entry_type=&batch_id=` | 9 | List queue entries (default `pending`), optionally only what one batch left | 200 | 404 unknown batch |
| `PATCH /review/{id}` | 9 | Resolve: `confirmed_value` or `created_template_id` | 200 | 404, 422 (missing the field for that entry type) |
| `GET /review/groups?by=field or value or document, &batch_id=, &template_id=, &include_rejected=, &limit=, &offset=` | 9+ | Pending entries grouped: **one field across documents** (`field`), the same value across documents (`value`), or every pending field of one document. `include_rejected` adds Module 7 rejections, each group saying whether it can be accepted | 200 `{by, total, groups[]}` | 422 (bad `by`) |
| `POST /review/bulk` | 9+ | Accept many spot checks; server-enforced eligibility; random audit sample held back | 200 `{accepted[], held_for_audit[]}` | **422** `{refused: {id: reason}}` (nothing applied) |
| `POST /review/bulk-correct` | 9+ | One typed value for every entry of a value group; must fullmatch the field's regex; 10% held back as audit; `via = bulk` | 200 | **422** (value fails the field's format, mixed fields or versions, an audit sample) |
| `GET /review/agreed?batch_id=` | 9+ | How many spot checks the model read **and** an independent check backed (what the server would accept) | 200 | |
| `POST /review/accept-agreed` | 9+ | Bulk accept exactly those, with the audit hold. The client sends no ids: the server picks | 200 `{accepted[], held_for_audit[]}` | |
| `POST /review/clear` | housekeeping | Delete **pending** review entries: all, one `batch_id`, and/or one `field_id` (Leave blank). Body `{"confirm": true}`. Resolved entries stay | 200 `{deleted, by_type}` | 422 without `confirm`, 404 unknown batch |
| `GET /review/evidence?target=&confidence=` | 9+ | Per (template, version, field): checked, changed, upper bound, trusted | 200 | |
| `POST /review/field-review`, `POST /review/new-template-needed` | 9 | Manual enqueue (used by tests and tooling) | 201 | |

### 4.3 The JSON deliverable

What a downstream system reads from `GET /results/{id}` (`CompiledResult`):

```jsonc
{
  "template_id": "air_waybill",
  "version": 8,
  "extracted_at": "2026-10-29T10:14:03Z",
  "overall_status": "needs_review",         // "complete" only when nothing is flagged
  "fields": {
    "awb_number": {
      "field_id": "awb_number",
      "value": "176-06067736",
      "confidence": 96.0,                    // Tesseract word confidence, or 100 for PDF text
      "needs_review": false,
      "approval_tier": "auto",              // auto | spot_check | review  (machine's call)
      "auto_basis": null,                    // "trusted_field" when audited auto-approval promoted it
      "verified_by": null,                   // "person" | "bulk" once a human settled it
      "reasons": []                          // e.g. ["format_invalid", "check_digit_invalid",
    },                                       //       "cross_check_failed:<label>", "low_confidence"]
    "gross_weight": { "...": "..." }
  }
}
```

A consumer should treat only `approval_tier == "auto"` (or a field a human has resolved) as usable without review.

#### The batch export (`GET /documents/batch/{id}/export`)

For a person who wants one file per upload, not one call per result. Every field carries a **status** saying why it may be trusted:

| Status | Meaning | Emitted in strict mode |
|---|---|---|
| `confirmed` | a person resolved it one at a time, or corrected it | ✔ value |
| `accepted` | accepted from a group (bulk), so not an independent look | ✔ value |
| `auto` | validated and at or above the threshold, or trusted by audited auto-approval | ✔ value |
| `unverified` | nobody checked it and no rule vouched for it (including **every** value the vision model read until someone settles it) | **`null`**, with the machine's reading under `suggested` |

```jsonc
{ "batch_id": "618b37b3…", "strict": true, "generated_at": "2026-11-03T…",
  "summary": { "documents": 50, "fields": 1518, "emitted": 0, "withheld": 1518,
               "emitted_basis": { "confirmed": 0, "accepted": 0, "auto": 0, "unverified": 1518 }, "unrecognised_pages": 4 },
  "documents": [ { "filename": "demo_b_00__digital.pdf", "status": "completed",
    "pages": [ { "recognised": true, "template_id": "air_waybill_bkk", "template_version": 3, "reader": "vlm",
      "complete": false,
      "fields": { "awb_number": { "value": null, "status": "unverified", "confidence": 89.0,
                                  "suggested": "176-97333504", "reasons": [] } } } ] } ] }
```

`strict=false` emits every value with its status. Nothing is guessed in either mode: a page whose type was not recognised is listed as `{recognised: false, reason}` with no fields. Reviewing is what turns `withheld` into `emitted`; that is why the review shape of §2.5 matters.

### 4.4 Error-handling conventions and failure strategy

**Wire format.** FastAPI `HTTPException` bodies are `{"detail": ...}`, where `detail` is one of:

| Shape | Source | Frontend handling (`api.js`) |
|---|---|---|
| a string | 404s, simple 422s | one line |
| `{reason_code, message}` | ingestion failures, batch rejections | shows `message` |
| a list of `{loc, msg}` | Pydantic validation (e.g. a bbox outside 0..1) | one line per item: `loc.path: msg` |

The frontend wraps all of them in `ApiValidationError(status, detail)` and flattens them with `errorLines()`.

**Failure classification: the two kinds are not alike.**

```mermaid
flowchart TD
    F(["A document job fails"]) --> K{"What failed?"}
    K -- "IngestionError<br/>wrong type · too big · corrupt" --> P1["<b>failed on attempt 1</b><br/>NOT retried:<br/>same bytes fail the same way"]
    K -- "anything else<br/>DB blip · Tesseract · timeout" --> RT{"attempts ≤ 3?"}
    RT -- yes --> R1["<b>retrying</b><br/>wait 10 s → 60 s → 300 s<br/>(RQ scheduler)"]
    R1 --> RES["Resume at the <b>first unfinished page</b><br/>(progress saved after each page;<br/>no duplicated results or queue rows)"]
    RES --> OK(["completed"])
    RT -- no --> P2["<b>failed</b> with error text"]
    P1 --> DROP["payload bytes dropped"]
    P2 --> DROP
```

Other failure rules:

- **RQ runs a job's failure callback on *every* failed attempt**, so `on_failure` cannot mean "finally failed". The job asks `get_current_job().should_retry` instead.
- **Partial enqueue**: if Redis dies mid-submit, documents already queued still run; the rest are marked `failed` with `could not be queued: …` and the API returns **503** naming how many were queued.
- **A reader engine that is down is not an error either.** A dead SSH tunnel hangs rather than refuses, so connecting has its own 10 s limit (without it every page waited the full 120 s before the Tesseract fallback ran: 130 s a page). The page is read with Tesseract, reported as `ocr_fallback`, and capped at `spot_check`.
- **Unmatched type** is not an error. It is a normal outcome (`PageResult.confirmed = false` + a `new_template_needed` queue entry).
- **Page detection finding no quad** is also normal (a scan has no border). The refusal that matters is downstream, in Module 5's anchors.
- **A check that cannot decide** (input missing, empty or unparseable; a `when` gate closed) is `skipped`, never `passed`.

---

## 5. Security, auth & environment

### 5.1 Authentication and authorization: **⛔ none today**

There is **no authentication, no authorization, no user model, no CORS middleware and no rate limiting.** (A search of `app/` and `frontend/src/` for auth/JWT/OAuth/CORS/middleware finds nothing outside unrelated words.) Anyone who can reach port 8080 or 8000 can upload, read every result and review crop, edit values, and save templates. That is acceptable only on a trusted local network or developer machine. **It is the largest gap between this repository and a production deployment.**

What exists instead of CORS: the browser only ever talks to **its own origin**, because nginx (container) or the Vite dev proxy forwards the API prefixes. The API is therefore never called cross-origin by the UI.

**Proposed** (not built), in the order it should be done:

```mermaid
flowchart LR
    U["Browser"] -->|"OIDC login<br/>(e.g. Keycloak / Entra / Auth0)"| IDP["Identity provider"]
    IDP -->|"access token (JWT)"| U
    U -->|"Bearer token"| GW["nginx / API gateway<br/>TLS terminate · rate limit"]
    GW --> API["FastAPI<br/>verify JWT, map claims → role"]
    API --> RBAC{"role"}
    RBAC -- "operator" --> A1["upload · read results · resolve review"]
    RBAC -- "template-author" --> A2["+ POST /templates"]
    RBAC -- "admin" --> A3["+ archive · delete data"]
```

Minimum viable steps: TLS at the proxy; an OIDC/JWT dependency on every router; three roles (operator, template-author, admin); per-tenant scoping on `templates` / `extraction_results` / `batches` if more than one customer is ever served; an audit trail of who corrected which value (today `PATCH /results/.../fields/...` records the change but not the person).

### 5.2 Input handling and data protection (what *is* built)

| Control | Implementation |
|---|---|
| File-type check | libmagic content sniffing (`python-magic`), not the extension; allowed: `application/pdf`, `image/jpeg`, `image/png` |
| Size / page limits | 15 MB per file, 20 pages, 50 files per batch; nginx `client_max_body_size 800m` (= 50 × 15 MB, headroom) |
| Decompression-bomb guard | `MAX_UPSCALE_FACTOR = 2.0`; phone photos' meaningless 72-DPI tag once triggered a decompression-bomb crash |
| Server-side re-validation | Template bounding boxes are re-checked on the server even though the designer UI already checked them |
| No code evaluation | Cross-field checks are declarative (`product`, `sum`, `equal`, `one_of`, `maps_to`); **nothing is `eval`'d**, nothing is learned |
| No raw SQL from input | SQLAlchemy parameterised queries |
| Subprocess surface | Tesseract is invoked with image data via `pytesseract`; no user-controlled command text |
| Reader engine traffic | The field crops of a page (about 87 per scanned page) are sent to the reader engine, so **business-document content leaves the host**. The client sends `Authorization: Bearer` from `token_env`; the service port must be private (an SSH tunnel or a VPN), with TLS in front if it is reachable otherwise. Crops are not stored by the client; what the engine keeps is the engine operator's policy |
| Upload retention | Upload bytes are `NULL`ed from `batch_documents` once a document is terminal; review JPEGs (≤ 1400 px) **are** kept on `extraction_results` |
| Personal data in the repo | `tests/fixtures/` (an ID-card photo, a waybill with third-party contact data), `data/` and `backups/` are git-ignored and excluded from Docker images (`.dockerignore`) |

Data you should treat as sensitive in a real deployment: `extraction_results.fields` (the extracted values), `extraction_results.page_image` (a picture of the page), and the DB dumps in `backups/`.

### 5.3 Secrets and environment variables

There is **no `.env` file and no secrets manager**: configuration is read from process environment variables with development defaults in `app/config.py`.

| Variable | Read by | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | `app/config.py`, Alembic, tests | `postgresql+psycopg://idp:idp@localhost:5432/idp_pipeline` | SQLAlchemy URL. **Contains the password**; compose sets `idp:idp` for the throwaway dev DB |
| `REDIS_URL` | `app/config.py` | `redis://localhost:6379/0` | RQ connection |
| `TESSERACT_CMD` | `config.resolve_tesseract_cmd` | env → default Windows install path (file check) → `shutil.which("tesseract")` | Path to the Tesseract binary |
| `OMP_THREAD_LIMIT` | `runtime.configure_tesseract` | set to `1` if unset (`TESSERACT_OMP_THREAD_LIMIT`) | Caps each Tesseract process's threads (see §6.2) |
| `APP_PORT` | compose | `8000` | Host port for the API |
| `FRONTEND_PORT` | compose | `8080` | Host port for the UI |
| `API_TARGET` | `vite.config.js` | `http://localhost:8000` | Where the dev proxy forwards (e.g. the Docker API on `:8010`) |
| `VLM_READER_URL`, `VLM_READER_TOKEN` | `app/config.py`, `vlm_reader/engines.py` | unset = reader **off** | The single reader service when no registry file is used (engine `default`); the token is sent as a bearer token |
| `READER_ENGINES_FILE`, `READER_ENGINE` | `vlm_reader/engines.py` | unset | The JSON registry of named engines and which one is active (§2.9). Tokens inside it are env-var **names** (`token_env`) |
| `VLM_READER_TIMEOUT_S`, `VLM_READER_CONNECT_TIMEOUT_S`, `VLM_READER_CROPS_PER_REQUEST` | `app/config.py` | 120, 10, 24 | Per-request limits (a service answers 24 crops in ≈ 7 s); the short connect limit is what stops a dead tunnel from costing 130 s a page |
| `VLM_READER_PROFILE_DIR` | `vlm_reader/profile.py` | `data/reader_profiles` | Where the per-form reader profiles are (git-ignored, bind-mounted read-only by compose) |
| `VLM_GATED_CONFIDENCE`, `VLM_UNGATED_CONFIDENCE` | `app/config.py` | 89, 70 | The fixed confidences a vision-read value gets (§2.4); 89 is under the 90 auto threshold on purpose |
| `IDP_FORM`, `IDP_LANDMARKS` | experiment scripts only | `B`, none | Select a waybill layout / a train-only landmark list; **not** read by the app |

Environment safety rules built into the code: the root `conftest.py` sets `DATABASE_URL` to the separate **`idp_pipeline_test`** database with `setdefault` *before any `app.*` import*, so tests can never write into the dev database; the test compose service sets it explicitly for the same reason.

**Proposed**: move `DATABASE_URL` and any future token-signing keys to Docker/Kubernetes secrets or a vault, rotate the `idp:idp` credentials, and stop publishing Postgres (`5433`) and Redis (`6380`) on host ports outside development.

---

## 6. Non-functional requirements & scale

### 6.1 Targets: stated versus measured

| Requirement | Stated target | Where it stands |
|---|---|---|
| **Error rate of auto-approved values** | At most **1 in 10,000** (the user's wording; whether "per value" or "per document" was never confirmed) | **Not demonstrable with the data in hand.** 0 wrong among *N* gated values only bounds the true rate by about **3/N** at 95% confidence (rule of three). The synthetic corpus is renderings of one waybill (29 distinct values), so its 100% auto precision cannot show 99.99% |
| **Throughput** | "10,000+ documents" per batch campaign | See §6.2; ≈ 18 docs/min on a single CPU-bound host ⇒ 10,000 documents ≈ **9 hours**. Needs more hosts, not more workers on one |
| **Throughput with the vision reader on** | Not specified | **≈ 29 documents an hour** on one shared GPU (≈ 40 s of GPU time a page, ≈ 87 crops at ≈ 0.4 s each), against ≈ 18 a minute for the Tesseract path. 10,000 scanned documents would be ≈ 345 hours on that one card: GPU capacity, not CPU, is the limit when the reader is on |
| **Concurrency** | Not specified | Bounded by CPU (see below), not by the API |
| **Availability / RPO / RTO** | Not specified | ⛔ Single Postgres, single Redis, no replication, no backup job. Manual dumps exist in `backups/` |
| **Latency** | Not specified | Interactive path is `POST /documents/process` (sync, up to a 300 s proxy timeout); the product path is the asynchronous batch |

### 6.2 Measured performance (all from comments in `app/config.py` / `CLAUDE.md`)

**Batch throughput**: 22-document corpus in one `POST /documents/batch` on the containerised stack, wall time from submit to last document (so it includes a tail):

| Workers | 1 | 2 | 3 | 4 | 6 |
|---|---|---|---|---|---|
| docs / minute | 10.3 | 16.2 | 17.5 | 18.1 | 17.8 |

```mermaid
xychart-beta
    title "Batch throughput vs worker count (docs/min, 22-doc corpus)"
    x-axis ["1", "2", "3", "4", "6"]
    y-axis "docs per minute" 0 --> 20
    bar [10.3, 16.2, 17.5, 18.1, 17.8]
```

It plateaus at about 18 from three workers because the machine is **CPU-bound**: per-document time grows in step, so more workers on one host buy nothing. Scale-out means more hosts.

**The Tesseract OpenMP trap** (the single largest performance fix): each field read is its own Tesseract process and a document runs 8 at once; by default each process also starts a thread per core and they spin-wait against each other.

| Measurement (18 vCPUs, Linux container) | Default | `OMP_THREAD_LIMIT=1` |
|---|---|---|
| One document's OCR stage | 8.7 s | **1.9 s** |
| Three documents at once | 186–191 s **each** | **4.2–4.8 s** |
| Throughput, 1 worker | 6.4 docs/min | 10.3 docs/min |

On Windows it makes no difference (2.4 vs 2.5 s). `runtime.configure_tesseract()` now sets the cap for the API, the worker and the scripts; results are identical with and without it.

**Other measured costs**: page alignment adds one whole-page OCR (≈ 1.5 s per page) *only* for templates that carry landmarks; text-native PDF pages need no OCR at all and no second preprocessing pass.

**Accuracy** (fields correct; single reference waybill + synthetic renderings; **real photographed pages are mostly unmeasured**): full 22-document corpus through the batch API, 22/22 classified correctly with no hint, 584/612 fields correct (text-layer 58/58, scanned 252/261, camera 274/293); auto-approved precision 100% in each tier, and all six accepted-wrong reads landed in `spot_check`. Auto share after the "no validator ⇒ never auto" rule: text-layer 66%, scanned 52%, camera 51%. On 16 *real* waybills from several printers, page alignment lifted first-page anchor recognition from **1 of 16 to 12 of 16**; field accuracy on non-reference layouts is much lower (see §8).

**The vision reader, measured** (the numbers below are from this repository's own runs; the labels for the real pages are unverified drafts, and the synthetic documents share two layouts with the adapter's training data, so they measure content and scan quality, **not unseen forms**):

| Run | Result |
|---|---|
| **Real held-out pages**, round-4 adapter | **89.1%** of fields right (previous adapter 89.7%: no measured gain on real new pages). The old Tesseract pipeline gets **11%** on the same pages |
| Synthetic validation (adapter chosen on this only) | **94.6%** (previous adapter 92.9% on the same crops) |
| Trial through the running API, real scanned Bangkok-form pages, with a type hint | 27–33 of 33 fields right per page, 33–69 s a page; 0.27–0.31 s a crop on a shared RTX 5090; saved research crops came back byte-identical (150 of 150) |
| **50 distinct hard Faker waybills in one batch, no type hint, 1 worker** (24 digital, 18 scanned, 8 camera photos) | 46 identified (25 as `air_waybill_bkk`, 20 as `air_waybill` by the model, 1 by Tesseract; 4 refused `no_match`), **1,730 s wall** |
| Same batch, fields right (the printed `HS CODE:` / `INV:` label the template drops on purpose is ignored when scoring) | **1,401 of 1,518 = 92.3%**: digital 760/792 (96.0%), scans 486/528 (92.0%), photos 155/198 (78.3%) |
| Same batch, where the wrong ones sit | **nothing at `auto`**, 58 wrong values in `spot_check`, 59 in `review` |

```mermaid
xychart-beta
    title "Fields right by input kind, 50-document hard batch (%)"
    x-axis ["text-layer PDF", "scanned PDF", "camera photo"]
    y-axis "fields right" 0 --> 100
    bar [96.0, 92.0, 78.3]
```

What the fixes in the last days were worth, each measured on the batch or pages named:

| Change | Before → after |
|---|---|
| A **doubtful text layer** goes to the model (24 hard digital pages) | 320 → 738 of 792 fields right; wrong values at `auto` 10 → 0 |
| The reader **reuses Module 5's alignment** when its own refit fails (8 noisy scans and photos) | 26 → 206 of 264 fields right; 7 of the 8 pages moved from Tesseract to the model |
| `air_waybill` **yields to** `air_waybill_bkk` | every Bangkok scan was `ambiguous_match` and never reached the model; 9 of 9 now resolve |
| **Cap `ocr_fallback` pages at `spot_check`** | 4 wrong values at confidence 93–96 had reached `auto`; none now |
| Scoring ignores the dropped printed label | 1,361 → 1,401 right: the 40 "misses" were label-only differences, not misreads |

**Reading these honestly:** every value in the batch above needs a person, because the model's fixed confidence is under the auto threshold. What a person has to catch is the 58 + 59 wrong values among roughly 670 queued ones; the gated 89 values are right about 99.5% of the time, which is the figure to weigh against "1 in 10,000" (§6.1).

### 6.3 Scalability limits

```mermaid
flowchart LR
    subgraph now["Scales today"]
        A["API: stateless,<br/>horizontally replicable<br/>(migration race: see below)"]
        B["Workers: stateless,<br/>--scale worker=N"]
    end
    subgraph bound["Limits you will hit, in order"]
        C["① CPU per host<br/>≈ 18 docs/min plateau"]
        D["② Postgres as blob store<br/>upload bytes + page JPEG per page<br/>→ database growth, WAL volume"]
        E["③ Single Redis, single Postgres<br/>no HA, no replicas"]
        F["④ Per-page template reads<br/>(no cache)"]
    end
    now --> C --> D --> E --> F
```

- **Multiple API replicas racing to migrate**: the container `CMD` runs `alembic upgrade head` at start. The Dockerfile notes this should become an init container/one-shot job before running more than one replica.
- **Blob storage in Postgres** is a deliberate simplicity trade (API and worker share only the DB). At high volume, move uploads and review JPEGs to object storage (S3/MinIO) and keep a key in the row.
- **Worker scaling beyond 6 is untested**; Redis is verified only at Redis 7 inside the compose stack, single instance.
- **GPU capacity** is the first limit once the reader is on: ≈ 29 documents an hour per shared card, and the crops leave the host. More throughput means more engines or a faster one (the registry makes adding one a config change, §2.9); the OpenAI-compatible reader already sends several crops at once.
- **Windows workers** fall back to `SimpleWorker` (no fork), so a hung job is only cut off by a timer thread. Use the Linux image for production.

### 6.4 Logging, monitoring and error tracking

| Capability | State |
|---|---|
| **Application logging** | ✅ Python `logging`, logger `app`, INFO level, to stderr (`runtime.configure_logging`). Per page it logs the deskew angle, method and whether it was applied, and the alignment summary (landmarks matched, scale, shift, rms). Uvicorn's own loggers are separate |
| **Which reader read a page** | ✅ `PageResult.reader` (`text_layer`, `vlm`, `ocr`, `ocr_fallback`) is returned and stored in the batch result; the engine name and gate counts are logged (`vision reader waybill_B via engine qwen_waybill: 33 fields, 21 gated, 0 blank`). The engine is **not yet stored on the extraction result** |
| **Per-document audit** | ✅ `batch_documents` keeps `status`, `attempts`, `error`, `started_at`, `finished_at`; `extraction_results` keeps `template_id` + `template_version` + `extracted_at` |
| **Metrics** (Prometheus/StatsD) | ⛔ None |
| **Distributed tracing** | ⛔ None |
| **Error tracking** (Sentry etc.) | ⛔ None; exceptions land in container logs only |
| **Health check** | ✅ `GET /docs` for the app container; `pg_isready` and `redis-cli ping` for the stores. No dedicated `/health` or `/ready` endpoint |
| **Quality monitoring** | ✅ Offline scripts: `evaluate_accuracy.py`, `evaluate_batch.py` (through a running API), `evaluate_deskew.py`, `evaluate_forms.py`, **`evaluate_reader.py`** (the same documents through each reader engine: fields right by input kind, wrong values by approval tier, docs/min, and the two-engine agreement table), `export_batch_json.py` (a finished batch → per-field extract and a right/wrong scorecard) |

**Proposed** minimum: a `/healthz` + `/readyz` pair (DB + Redis checks), `prometheus-fastapi-instrumentator` plus RQ queue-depth and per-tier counters (auto / spot_check / review rate is the single most useful business metric), Sentry for exceptions, and structured JSON logs carrying `batch_id`, `document_id` and `template_id` on every line.

---

## 7. Architecture decision records (ADRs) / trade-offs

| # | Decision | Chosen | Over | Why | Cost / risk accepted |
|---|---|---|---|---|---|
| 1 | Extraction approach | **User-marked coordinate templates**; the PDF text layer, a vision model reading template-guided crops, and Tesseract as the fallback | ML layout models, LLM-only whole-page extraction | Predictable, auditable, every miss is explainable; the template says where, so the model only has to read a crop and cannot invent a location; matches "refuse and flag" | Brittle when a form drifts: needed alignment (ADR 5) and still needs one template per printed layout; the model needs a GPU machine |
| 2 | Architecture style | **Modular monolith** (API + worker from one image) | Microservices per module | One team, one deployable, modules are plain function calls, trivial to test | Cannot scale modules independently; OCR-heavy work shares a process type with nothing else |
| 3 | Durable state | **Postgres for everything durable**, uploads stored on the row; Redis only as a queue | Shared disk / S3 for uploads; Redis for job state | API and worker share only the DB; RQ job state is short-lived but batch status must persist | Database holds binary blobs (growth, WAL); fine at current scale, revisit at volume |
| 4 | Templates | **Append-only versions**, one mutable flag (`is_archived`) | Edit-in-place | Past results reproducible against the exact template that ran; no accidental regression of old data | More rows; consumers must always pass the exact version |
| 5 | Page drift | **Landmark-word alignment** (RANSAC scale+shift per axis + local median), refuse below 12 words | Fixed percentage boxes; template-matching of the whole page; a model | Of 16 real waybills only 1 matched its anchors before; 12 after. Deterministic, seeded, returns `None` instead of guessing | OCRs the whole page (≈ 1.5 s); provisional constants measured on little data; a different printer's form still needs its own template |
| 6 | Document-type matching | **All anchors must pass (AND)**; two matches = `ambiguous_match` | Best-score pick | Never silently choose between layouts | Boilerplate anchors (IATA text) match other vendors' blank forms; anchors must be label text unique to the layout |
| 7 | Preprocessing | **Two passes** (generic, then template hints) and **two renderings** per page (binarized for anchors, grayscale for fields) | One normalization for everything | The type is unknown at pass 1; binarization finds headers but shreds glyph strokes | Extra cost only when a template declares hints |
| 8 | Text-native PDFs | **Read the embedded text layer, skip OCR** | OCR everything | Exact, instant, confidence 100 | Depends on the PDF producer's font metrics (a template cut from one producer's PDF is not guaranteed on another's); invisible OCR text must be excluded |
| 9 | Approval policy | **Three tiers**; a field with **no validator is never `auto`** | Confidence alone | Every wrong accepted value in the corpus was in an unvalidated field at confidence 64–87 | 186 correct reads held for human glance (auto share 51–66%) |
| 10 | Deskew order | **Contour → Hough → OSD (last)**, **keep the frame** | OSD first; grow the canvas | OSD only sees 90° steps (said 0 for 1.5° and 2.5° skew); a growing canvas shifted every percentage box and *hurt* accuracy (1.5° skew: 8/29 fields right uncorrected, 7/29 with the growing rotation, 25/29 with the frame kept) | Skew beyond 10° is left alone and logged |
| 11 | Cross-field rules | **Declarative checks** (`product`, `sum`, `equal`, `one_of`, `maps_to`) | An expression language | Nothing evaluated as code; a check that cannot decide is `skipped`, never `passed` | New rule shapes need code |
| 12 | Tesseract parallelism | **8 field reads per document, `OMP_THREAD_LIMIT=1` per process** | Default threading | 8.7 s → 1.9 s per document; 3 documents at once 186 s → 4.5 s | Slightly different reads on Debian vs Windows Tesseract at marginal values |
| 13 | Batch retries | **Don't retry ingestion errors; retry the rest 3× with 10/60/300 s; resume per page** | Retry everything; restart the document | Same bytes fail the same way; a finished page already wrote rows | Delayed retries need the RQ scheduler (and Lua on Redis) |
| 14 | Frontend delivery | **Static SPA behind a same-origin proxy**, hash routing, polling | SSR; cross-origin API + CORS; WebSockets | No CORS surface, trivial hosting, no server state in the UI | Two proxy configs to keep in sync; polling instead of push |
| 15 | Auth | **Deferred** | Building it up front | Single-user local tool during Phase 1 | **Must be done before any shared or internet-facing deployment** (§5.1) |
| 16 | Read errors | **Never "correct" a read**; edge-trimming of rule fragments only (and template-declared selection) | Post-hoc spell/OCR correction | A confident wrong value is the worst outcome; corrections would hide misreads | More values land in review |
| 17 | Review volume | **Grouped view + server-enforced bulk accept + random audit sample** | A client-side "accept all" button; reviewing every row | The UI cannot be the safety net; an audit sample keeps bulk-accepted values measured | A person can still wave a crop sheet through; the audit sample is what catches that statistically |
| 18 | Reducing human review | **Audited auto-approval from an exact binomial bound**, per template version and field | A fixed confidence threshold; trusting a model's self-reported confidence | A threshold is only meaningful once calibrated against checked data; the bound says in numbers how much checking is still missing; one error withdraws trust | Nothing is trusted until 29,956 clean checks per field (at 1 in 10,000); needs honest individual reviews as the evidence |
| 19 | Template relationships | **Derive whole-vs-part from anchor containment at match time** (most-specific wins) | Storing a parent/child link; letting a part reference the whole | Parts stay mutually independent templates; nothing to keep in sync | A part whose anchors are not a subset of the whole's still collides, and stays `ambiguous_match` |
| 20 | Anchor strictness | **All anchors by default; optional N-of-M, never under 2** | Always strict; a score-weighted pick | A smudged label should not refuse a page, but one label alone must not recognise one | N-of-M widens what can match, so ambiguity checks matter more |
| 21 | Vision-reader gate | **A value is gated only if (a second reader agrees OR an arithmetic fact holds) AND its format passes; gated gets a fixed 89, anything else 70, both under the 90 auto line** | Trusting the model's answer; its self-reported confidence (it reports none) | A prompt that contained a valid example number was copied by the model on clipped crops (found and removed); agreement is what survives that. Gate precision measured 0 wrong of 236 on real pages but 2 of 955 on synthetic arithmetic-gated fields | Every model value is a spot check until someone raises the number or trust is earned; the review burden is real (§2.7) |
| 22 | Which model reads | **A named engine in a registry file**, two kinds (our `/read` service, any OpenAI-compatible server) behind one all-or-nothing call | A client hard-wired to one model; one model per template in code | A newly published model should be a config entry plus a scorecard, not a release; both kinds look identical to the pipeline | The question wording is still the shipped adapter's, each engine's trust must be measured, and the engine is not yet stored per result |
| 23 | Reader failure | **Fall back to Tesseract, say so (`ocr_fallback`), cap the page at `spot_check`** | Fail the page; fall back silently | A dead GPU must not stop a batch, and the fallback measured 4 wrong values at confidence 93–96 | On scans and photos accuracy falls to Tesseract's until the engine is back |
| 24 | Doubtful text layers | **Route a text-layer page to the vision reader when Module 7 rejects anything on it** | Trust the layer whenever it exists | A layer that sits off its cells reads a neighbour's words and the alignment cannot see it: 320 → 738 of 792 fields right, wrong at `auto` 10 → 0 | ≈ 40 s of GPU per routed page |
| 25 | Second reader | **A second *model*, to be measured, not Tesseract** | Tesseract agreement as a gate | Tesseract agreed with the model on only 55 of 624 uncertain values and was right on 1 of 117 model errors | The queue stays near 670 of 1,518 fields until a strong second engine is measured (§10.3) |
| 26 | JSON output | **Strict export: unverified values are `null`, with the reading beside them** | Emit everything with a flag | Downstream systems act on emitted values, so only checked or rule-vouched ones are emitted | A fresh batch exports mostly `null` until it has been reviewed |
| 27 | Review shape | **By field across one upload; correct a group; accept agreed; leave a field blank**, with server-side eligibility and an audit hold | One-by-one review; a client-side "accept all" | One field across documents is the cheapest thing for a person to scan; the safety stays on the server | Does **not** reach 20–30 items per 50 documents (§2.7); that needs a stronger second reader, not a nicer screen |


---

## 8. How the technology works right now

### 8.1 What is shipped and what is research tooling

```mermaid
flowchart TB
    P1["Ingestion → preprocess → anchors → alignment<br/>(the application, every page)"] --> Q{"Which reader?"}
    Q -- "text layer that validates" --> V7["Module 7: validate + tiers"]
    Q -- "scan or photo,<br/>template opts in, engine up" --> VR["Vision reader:<br/>crops → engine → staged answer → gate"]
    Q -- "engine down,<br/>or template not opted in" --> TS["Tesseract"]
    VR --> V7
    TS --> V7
    V7 --> OUT["Results · review queue · strict batch export"]
    VR -.-> ENG[["Reader engine (a registry entry, on a GPU machine):<br/>service = Qwen2.5-VL-7B + LoRA, or any OpenAI-compatible server"]]
    RES[["🧪 Research tooling, not the running app:<br/>make_train_set, synth_bkk, scripts/gpu trainer,<br/>score_samples, score_vlm, evaluate_reader"]] -.-> ENG
```

The previous version of this document called the vision reader experimental and not wired in. That changed: it is committed, wired into Module 6, opt-in per template and off until an engine is configured. What remains research is the *training* side.

### 8.2 Committed versus uncommitted

Latest commit: `6877a9a` *Reader engines are configuration: a registry, an OpenAI-compatible crop reader and a scorecard script*. Since the previous revision of this document the following landed, each as its own commit:

| Commit | What it added |
|---|---|
| `55a98f0`, `f7da552` | The vision-model reader wired into Module 6, and its experiment tooling |
| `d349d7d` | A 10 s connect timeout for the reader client (a dead tunnel cost 130 s a page) |
| `3829926` | `hints.yields_to`: a boilerplate-anchored template yields to the print whose own mark also passed |
| `5e37b5b` | A text-native page goes to the model when its text-layer read is doubtful |
| `d6c68ea` | `ocr_fallback` pages capped at `spot_check`; fallback pages aligned with Module 5's alignment |
| `99f56e5`, `7707b95` | Review scoped to a batch, correct a group at once, accept agreed model reads, clear queue and history; the Review screen redesigned |
| `ed6c42b` | `hs_code` and `invoice_reference` scored without the printed label the template drops |
| `c8a3bd0` | Review by field, Leave blank, `verified_by`, the strict JSON export |
| `6877a9a` | The reader-engine registry, the OpenAI-compatible adapter, `evaluate_reader.py` |

🟡 **In the working tree, not committed, and belonging to a separate piece of work:** measurement-based engine selection (`app/modules/engine_selection/`, `app/models/engine_selection.py`, migration `0007`, `ENGINE_*` settings, per-engine prompts, `scripts/export_labelled.py`, `tests/test_engine_selection.py`) and the small edits to `engines.py`, `client.py`, `openai_client.py`, `reader.py`, `waybill.py`, `pipeline.py`, `review_queue.py`, `output_compiler.py` and `config.py` that go with it. This document describes it only as in progress.

### 8.3 What the 16 real waybills showed

This table is the **template + Tesseract/text-layer pipeline** alone (fields right of 33 per document, landmark lists built from training pages only so no held-out page aligns itself). It is kept because it explains *why* the vision reader exists; the reader's own numbers are in §6.2.

| Layout | Document | Kind | Template pipeline | Page-OCR word reader (experiment) |
|---|---|---|---|---|
| Bangkok (train) | 08, 11 | text layer | **33 / 33** each | n/a |
| Bangkok (train) | 02, 06, 12 | scan | 8, 5, 6 | 12, 8, 11 |
| Bangkok (held out) | 01 | scan | 7 (**1 wrong value auto-approved**) | 13 |
| Bangkok (held out) | 03, 13 | scan | **refused** (anchor 43 / 54) | 13, 6 |
| Bangkok (held out) | 14 | scan | 1 | 1 |
| Mumbai | 09 / 10 (train) | text / scan | 31, 11 | 25 across both |
| Mumbai | 07 (held out) | text layer | 10 | 6 |

Reading it plainly: **clean text-layer PDFs are solved; scans of the same form were not**, because the drift is larger than the boxes tolerate. Through the application with the vision reader on and a type hint, the same scanned Bangkok pages read **27–33 of 33** fields (§6.2), and no model-read value was auto-approved.

### 8.4 State of the vision reader

- **Wired in:** `app/modules/vlm_reader/` is part of Module 6. It runs for a scanned or photographed page of a template that opts in, for a text-layer page Module 7 rejects something on, and falls back to Tesseract (capped at `spot_check`) whenever the engine cannot be used.
- **Model:** Qwen2.5-VL-7B-Instruct with a LoRA adapter, chosen on synthetic validation only. The adapter in use is `data/models/wb_v4_epoch3` (309 MB, local); the earlier `wb_v3_epoch2` is kept. Synthetic validation 94.6% (previous adapter 92.9%); real held-out pages 89.1% (previous 89.7%), i.e. no measured gain on real new pages. The old Tesseract pipeline gets 11% on the same pages.
- **Found and fixed on the way:** a prompt that contained a valid mod-7 example number was **copied** by the model on clipped crops (examples removed, training sets rebuilt); a crop that cuts a value off is answered EMPTY in training, never with the visible part (this fixed the model writing `123,456.00` into caption-only crops); a boilerplate-anchored template made every Bangkok scan `ambiguous_match` before the model could run (`yields_to`); a dead tunnel blocked a worker for the whole read timeout (separate connect timeout); the Tesseract fallback let wrong values reach `auto` (cap).
- **Engines:** the model that reads is a registry entry (§2.9). Both shipped kinds answer identically to the pipeline. **No second model has been scored yet.** `scripts/evaluate_reader.py` is the instrument that will do it.
- **What the GPU is for:** *running* the reader on scanned pages. Training is finished and nothing about a text-layer PDF that validates needs it. The development GPU machine is a shared lab box that is not part of this project's footprint; the application is only its client.
- **Not done:** a reader that works on a **layout it has never seen** (the central open problem, §10); layouts with no template or synthetic generator (SalamAir, Pioneer); storing the engine name on each result; per-engine prompts (in progress, §8.2); fine-tuning Tesseract itself.
- **Throughput caveat:** ≈ 0.27–0.4 s per crop and ≈ 87 crops per scanned page is ≈ 40 s of GPU time a page, ≈ 29 documents an hour on one shared card (§6.1).
- **Security caveat:** the crops leave the host. A bearer token, a private port and TLS in front are required before real documents are sent; the service token created for the trial has not been rotated.

### 8.5 Known gaps and next steps (priority order)

1. **Authentication/authorization and TLS** (§5.1): required before any shared deployment.
2. **Get a GPU the project controls** and run the **engine bake-off**: the current adapter, Baidu Unlimited-OCR, PaddleOCR-VL and any other candidate, scored by `evaluate_reader.py` on the 50-document hard batch and the real pages. Decide the default engine and whether a second engine can serve as an independent reader (§10.3).
3. **Verify the draft labels** in `data/real_awb/labels/` (they are unverified; every accuracy figure on real documents depends on them).
4. **Shrink the queue honestly.** ≈ 670 of 1,518 fields on a 50-document batch stay queued at the target error rate (§2.7). The levers that are left are a validated second reader, master-data validators (§10.3) and a grounding check, not another screen.
5. **Store the engine on each result** and make trust evidence engine-aware, so switching the model cannot silently inherit another model's evidence.
6. **Observability**: `/healthz`, metrics, error tracking, structured logs (§6.4).
7. **Move blobs out of Postgres** and **split migrations from API start-up** before scaling out.
8. **More real data per layout**: a handful of test documents cannot certify 1-in-10,000; the project needs hundreds of labelled real pages per layout and a gate evaluated per auto-approved value.
9. **Auto-build landmarks at template-save time** in the designer (today `scripts/build_landmarks.py` is run by hand over other real documents).

---

## 9. Appendix

### 9.1 Repository map

```mermaid
flowchart LR
    root["idp-pipeline/"] --> app["app/<br/>FastAPI + pipeline"]
    root --> fe["frontend/<br/>React console"]
    root --> alembic["alembic/versions/<br/>0001–0006 (🟡 0007 in the tree)"]
    root --> scripts["scripts/<br/>seed, evaluate (incl. evaluate_reader),<br/>build corpora and the demo batch,<br/>export_batch_json, waybill_forms, 🧪 gpu/"]
    root --> config["config/<br/>reader_engines.example.json"]
    root --> data["data/ (git-ignored)<br/>reader_profiles, models, corpora"]
    root --> tests["tests/<br/>596 tests, real-OCR tests,<br/>fixtures/ git-ignored"]
    root --> docker["Dockerfile · docker-compose*.yml<br/>docker/postgres-init/"]
    root --> docs["CLAUDE.md · ACCURACY_REPORT.md<br/>idp-phase1-master-reference.pdf<br/>architecture.md"]
    app --> modules["modules/ (the 9 modules + batch + alignment + checks + pipeline<br/>+ vlm_reader/ + review_groups + trust + export + housekeeping)"]
    app --> models["models/ (SQLAlchemy)"]
    app --> schemas["schemas/ (Pydantic)"]
    app --> cfg["config.py (every tunable, with its measurement)<br/>db.py · job_queue.py · runtime.py · worker.py"]
```

### 9.2 Technology stack at a glance

| Layer | Technology |
|---|---|
| Frontend | React 19, Vite 8, Tailwind CSS 4, shadcn-style components (Radix Slot, class-variance-authority), Fabric.js 7 (template canvas), framer-motion, lucide-react, IBM Plex fonts; oxlint |
| Reverse proxy | nginx (container), Vite dev proxy (development) |
| Backend | Python 3.13, FastAPI, Uvicorn, Pydantic 2, SQLAlchemy 2, Alembic, psycopg 3 |
| Document handling | PyMuPDF (PDF render + text layer), Pillow, OpenCV (headless), NumPy, python-magic, rapidfuzz |
| OCR | Tesseract 5.5 via `pytesseract` (`eng`, tessdata_fast) |
| Queue | RQ over Redis 7 (`fakeredis[lua]` in tests) |
| Database | PostgreSQL 17 (JSONB, BYTEA) |
| Packaging | Docker multi-stage image (`runtime`, `test`), Docker Compose |
| Tests | pytest (real Tesseract in some), httpx; separate `idp_pipeline_test` database |
| Vision reader (opt-in) | Qwen2.5-VL-7B-Instruct + LoRA served by `scripts/gpu/serve_reader.py` (FastAPI, on a GPU machine), or any OpenAI-compatible server (vLLM, SGLang, Ollama, hosted); `httpx` client; engines named in `READER_ENGINES_FILE` |
| 🧪 Research | PEFT LoRA training scripts, shared GPU box over SSH |

### 9.3 Commands

```bash
# the whole stack in containers (run in WSL or anywhere with Docker); UI on :8080
docker compose up -d --build
docker compose exec app python scripts/seed_templates.py     # a fresh DB has no templates
docker compose up -d --scale worker=3                         # more batch workers

# local development on Windows (use the project venv, not base conda)
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000   # no auto-reload
npm --prefix frontend run dev -- --port 5173
.\.venv\Scripts\python.exe -m app.worker                          # needs Redis

# the model that reads: pick an engine (config/reader_engines.example.json), then score it
$env:READER_ENGINES_FILE = "config\reader_engines.json"; $env:READER_ENGINE = "qwen_waybill"
.\.venv\Scripts\python.exe scripts\evaluate_reader.py --images-dir data\demo_50 --ground-truth data\demo_50\ground_truth.json --engine tesseract --engine qwen_waybill
.\.venv\Scripts\python.exe scripts\export_batch_json.py --api http://localhost:8000 --batch-id ID --out data\demo_50_results --wait

# quality
.\.venv\Scripts\python.exe -m pytest -q
npm --prefix frontend run lint
node frontend/scripts/check_contrast.mjs
```

### 9.4 Glossary

| Term | Meaning |
|---|---|
| **Template** | A saved, versioned markup of one document layout: anchors, fields, landmarks, checks, hints |
| **Anchor** | A printed label whose text and position identify the layout; *all* must match |
| **Field** | A box (page fractions) whose content is extracted, with its format rules |
| **Landmark** | A printed word that recurs at a consistent position across real documents of the layout; used to re-align boxes on a shifted or rescaled page |
| **Alignment** | The per-axis scale+shift (plus local correction) that maps template boxes onto this page |
| **Text-native page** | A PDF page with a real, visible embedded text layer (not OCR output) |
| **PSM** | Tesseract page-segmentation mode (4, 6 and 7 are tried per field) |
| **Approval tier** | `auto` / `spot_check` / `review`: how much human attention a value still needs |
| **Cross-field check** | A declarative rule across several fields (e.g. weight × rate = total) |
| **Review queue** | Module 9's backlog of fields and unmatched pages needing a person |
| **Hint-only template** | A template used only with an explicit `type_hint`, excluded from the no-hint scan |
| **Reader engine** | A named entry in the registry file: which model answers the field crops, and how to reach it |
| **Reader profile** | A form's printed rules, zones and wording (`hints.vlm_reader`), distinct from the engine |
| **Gated value** | A model read that a second reader agrees with, or an arithmetic fact backs, and whose format passes; gets confidence 89, still below the auto line |
| **`ocr_fallback`** | The page wanted the vision reader, Tesseract read it instead; capped at `spot_check` |
| **Verified by** | Who settled a field: `person` (one at a time), `bulk` (accepted from a group) |
| **Strict export** | The batch JSON that leaves unverified values `null` and puts the machine's reading under `suggested` |
| **Audit sample** | A random share of a bulk action held back for one-by-one checking; only those count as evidence |
| **Accept agreed** | Bulk-accept the model reads that an independent check also backed (still 0.5% wrong on the hard batch) |

---

## 10. Research survey and recommended direction

> **How to read this section.** I searched for recent work on exactly the problems this project has. Most findings below come from **paper abstracts and search snippets, not full reads**, and several are preprints; numbers are quoted as the sources state them. Vendor claims (including the air-waybill vendors) are marketing, not benchmarks. Treat each item as a lead to verify against the paper, then against *your* documents.

### 10.1 The four problems you actually have

```mermaid
flowchart LR
    P1["<b>P1 Any layout</b><br/>a form the system has<br/>never seen, or only<br/>part of one"]
    P2["<b>P2 Accuracy you can prove</b><br/>1 error in 10,000 per<br/>auto value"]
    P3["<b>P3 Little human time</b><br/>2,000 documents must not<br/>mean 40,000 review rows"]
    P4["<b>P4 Staying right</b><br/>a new printer, a drifting<br/>scan setup, a trusted field<br/>that stops being right"]
    P1 --- S1["template induction ·<br/>grounded readers ·<br/>synthetic layout variety"]
    P2 --- S2["calibrated gate ·<br/>exact bounds ·<br/>real gold set"]
    P3 --- S3["more validators ·<br/>grouping · audited trust ·<br/>active selection"]
    P4 --- S4["drift monitoring ·<br/>auto-withdraw trust"]
```

### 10.2 What the literature says, and what it means here

| Theme | Finding (as reported by the source) | What it means for this project |
|---|---|---|
| **Calibrated gates beat self-reported confidence** | [Perception, Layout, and Validation](https://arxiv.org/pdf/2609.20110) fuses perception, layout and **validation** signals into one decomposed score and applies conformal risk control: AUROC for separating right from wrong rises from 0.54–0.74 (a VLM's verbalised confidence) to 0.90–0.99, and with an error target under 10% the model's own confidence clears 0.1–7.0% of fields against 49–72% for the fused score (three public datasets) | Your gate today is rules (regex, check digit, agreement, arithmetic). A **fused, calibrated score per field type** would raise the auto share at the *same* proven risk. Validation signals are already your strongest feature |
| **Naive thresholding fails in three ways** | [Valid Per-Field Selective Risk Control](https://arxiv.org/abs/2608.14639): fields from one document are correlated (design effect 1.84–2.45); refitting the score on the calibration data broke the guarantee (violating α in 95% of splits in one setting); a score with heavy ties collapses the threshold grid. Its certified coverage at 10% risk was low (≈ 6–17% depending on variant) and it calls that near-vacuous | Keep calibration data **separate by document**, never refit on it, and expect certificates to need far more data than a pilot. Your ledger is one observation per document per field, which avoids the clustering problem, but **never pool fields across documents without correcting** |
| **Distribution-free guarantees exist** | [Learn then Test](https://arxiv.org/abs/2110.01052) casts threshold choice as multiple hypothesis testing with finite-sample guarantees; the conformal KIE paper ([IJDAR 2026](https://link.springer.com/10.1007/s10032-026-00572-y)) reports, on receipts at α = 0.02, 98.3% marginal coverage with 70% of predictions a single answer, and small reliable sets for dates and prices but large ones for ambiguous fields | Your Clopper-Pearson ledger is the simplest member of this family. Multiple tested thresholds per field type is the next step, not a rewrite |
| **Multi-signal confidence** | [Beyond Logprobs](https://arxiv.org/pdf/2606.24420) uses value agreement across calls, neighbourhood text overlap and spatial centroid divergence from OCR boxes, and flags values with **no grounding in any OCR token**; [SafePassage](https://arxiv.org/abs/2510.00276) grounds LLM outputs against the source text with an aligner (reported up to 85% fewer hallucinations); repeated-run consensus catches unstable values but costs latency and misses systematic mistakes | Exactly the failure you found (the copied example AWB): a **grounding check** (is the value actually present in the page OCR near that box?) is cheap, independent of the model, and catches inventions. Add it as a third signal beside "two readers agree" |
| **Whole-page parsers (Baidu Unlimited-OCR)** | [Unlimited-OCR](https://github.com/baidu/Unlimited-OCR) (MIT, v2 released 2026-10-27) is a long-document parser served with vLLM/SGLang: it turns a document into text, with **no word boxes** and no adapter for any form of ours | It cannot be a *page* reader here (the pipeline needs positions to cut fields) but can be tried as a **zero-shot crop reader** through the OpenAI-compatible adapter. It is a candidate until `evaluate_reader.py` scores it on our crops; its marketing says nothing about air waybills |
| **Small specialist readers** | On [OmniDocBench](https://blog.roboflow.com/best-open-source-ocr-models/) the top open models are 0.9–1.2B (PaddleOCR-VL-1.6 96.3, MinerU2.5-Pro 95.8, GLM-OCR 95.2) while a 235B Qwen3-VL scored ≈ 89 and olmOCR-7B 81.8 on an earlier version ([PaddleOCR-VL](https://arxiv.org/pdf/2606.03264)). No published invoice or waybill benchmark was found, and the sources are partly vendor-adjacent | A **≈ 1B specialist model could run on this laptop** and may beat a fine-tuned 7B at reading a clean crop. It must be tested on *your* 16 waybills before more 7B training |
| **Unseen layouts** | [VDInstruct](https://arxiv.org/pdf/2507.09531) argues earlier layout-aware models need task-specific fine-tuning and fail on unseen layouts, and targets zero-shot extraction; [PPN/P2Net](https://arxiv.org/pdf/2307.10551) does zero- and few-shot pointer-based extraction; a design-space study ([2502.18179](https://arxiv.org/pdf/2502.18179)) finds well-configured general LLMs can match specialised models; Google's Glean (2022) generalises with candidate generation, scoring and assignment | The literature's answer to "any waybill" is **candidate generation + scoring**, not one fixed box per field: find the printed caption, look to its right/below, propose candidates, score them. That is your caption-based localisation idea, and it has precedent |
| **Cheap labels** | [Selective labeling for visually rich documents](https://physics.paperswithcode.com/paper/radically-lower-data-labeling-costs-for) reports 10× lower labeling cost with negligible loss, but notes plain document-level active learning gave little over random; [NAT](https://arxiv.org/pdf/2404.00488) weak-labels a new type then adds a few human labels and synthetic documents; FieldSwap (ICDE 2024) makes synthetic examples by swapping key phrases between fields | Pick *which fields* to label (where readers disagree), not which pages. Swap captions between fields to synthesise the layout variety you lack |
| **Drift** | [Online shift detection with conformal abstention](https://arxiv.org/pdf/2606.11949) flags shift sequentially and tightens the threshold to hold an error target; [conformal test martingales](https://arxiv.org/abs/2602.13848) give anytime-valid detection; KMM-CP ([2603.26415](https://arxiv.org/pdf/2603.26415)) restores coverage under covariate shift. None is document-specific | You already log signals that move when a printer or scan setup changes: anchor scores, alignment inlier counts, reader agreement rate, confidence distribution. Monitor them per template and **suspend trust** on a shift |
| **Review practice** | Practitioner sources: calibrate per document type (a global curve masks differences); sample audits inversely to demonstrated reliability; tighten only after repeated clean audits; stratify audits toward costly errors; a fixed audit percentage is not meaningful by itself | This is what the audited auto-approval does, minus stratification. Add: audit **more** where a field's bound is barely under the target |
| **Air waybill market** | Vendors advertise 90–99%+ on AWB fields and note that carriers' layouts differ in where airline-specific codes sit, so fixed templates are unreliable; validation rules and confidence routing are the common selling points. No independent benchmark or test set was found | Your differentiator is *measured, per-value accuracy with a refusal path*, not a headline percentage. Publish the evidence table, not a claim |

### 10.3 Ideas, ranked by (value to you) ÷ (effort), each with what "done" means

```mermaid
quadrantChart
    title Ideas by effort and payoff
    x-axis "Less effort" --> "More effort"
    y-axis "Lower payoff" --> "Higher payoff"
    quadrant-1 "Plan carefully"
    quadrant-2 "Do first"
    quadrant-3 "Do when free"
    quadrant-4 "Avoid for now"
    "Grounding check (value in page OCR)": [0.15, 0.80]
    "Master-data validators": [0.25, 0.85]
    "Gold set of real pages": [0.45, 0.95]
    "Small specialist reader test": [0.30, 0.75]
    "Drift monitor + auto-withdraw trust": [0.40, 0.70]
    "Fused calibrated score": [0.60, 0.80]
    "Template induction from one example": [0.75, 0.90]
    "Active selection of what to label": [0.55, 0.55]
    "Train 7B on every layout": [0.85, 0.40]
```

1. **Master-data validators (highest payoff, lowest cost).** A field with no validator is never `auto`, and most waybill fields have no regex. Many of them *can* be validated against known lists: airline prefix to carrier (176 = Emirates; the full IATA prefix table), airport and IATA city codes, ISO currency, units of measure, incoterms, rate-class letters, HS code structure, AWB check digit (done), dates in range, weight × rate = total (done). Each validator turns a never-auto field into a field that can be `auto` with a *checked* shape. Done = the share of fields that have a validator is measured before and after, and no accepted wrong value appears in the corpus.
2. **Grounding check.** After any read (VLM or otherwise), require that the value's characters actually occur in the page OCR within the field's box (± a margin), using a fuzzy match. A value the reader *invented* has no ink to match. This directly closes the failure you found with the copied AWB. Done = on the clipped-crop test set the invented values are rejected and no correct value is.
3. **A gold set of real pages.** Everything else is limited by this. 16 documents cannot certify anything; the project needs hundreds of **verified** labels per layout, with a document-level split and the draft labels checked by a second person. Use the active-selection idea to label the fields where readers disagree first. Done = 200+ real pages per top layout, labels double-checked, frozen split.
4. **Benchmark other readers** (PaddleOCR-VL-1.6, GLM-OCR, Baidu Unlimited-OCR as a crop reader) through the engine registry on the same labelled documents as the current adapter: `evaluate_reader.py` prints fields right by input kind, wrong values by approval tier and docs/minute for each, and a second-reader table (where two engines agree, how often is it right). The instrument exists (§2.9); what is missing is a GPU to run candidates on. If a ≈ 1B model matches the 7B on crops you get a reader that runs without a GPU server, and if two strong engines agree you may finally get the independent second reader the queue floor (§2.7) needs. Done = that table for each engine.
5. **Drift monitor with automatic trust withdrawal.** Per template, track anchor scores, alignment inlier count, agreement rate and the confidence distribution with a simple sequential test; on a sustained shift, suspend `trusted_field` for that template and send its next N pages to review. Done = a simulated new-printer batch trips the alarm within a few documents.
6. **Fused, calibrated score per field type.** Train a small model (gradient-boosted trees or logistic regression) on per-field signals (OCR confidence, reader agreement, grounding, format pass, alignment quality, anchor scores) with the document-level split, calibrate it, and choose the auto threshold per field type with a Learn-then-Test style procedure. Done = a higher auto share than the rules at the same measured wrong-auto count on the gold set.
7. **Template induction from one example.** For a new layout, a document model proposes each field's box from the *caption text* (the printed label), a person confirms the boxes once in the designer, and the system derives anchors and landmarks automatically (the landmark builder exists as a script). This is the practical route to "any waybill": a new layout costs five minutes, not a new training run. Done = a new real layout reaches usable extraction after one confirmed sample.
8. **Layout-varying synthetic data.** Today's generators vary content and noise on *two* layouts. Add caption swapping, column shifts, fonts and printers (the FieldSwap idea) so a caption-based reader sees many layouts. Done = a reader trained on N layouts is measured on a held-out layout.
9. **Stratified audit and canaries.** Audit more where a field's bound is barely under the target, and occasionally insert a **known-wrong** value into the review stream to measure whether reviewers are actually looking (practitioner guidance on automation bias, not a peer-reviewed result). Done = reviewer catch rate on canaries is reported next to the evidence table.

### 10.4 What to do now: a sequence, not a menu

```mermaid
gantt
    title Recommended order
    dateFormat  YYYY-MM-DD
    axisFormat  %b %d
    section Decide with data
    Engine registry and scorecard (done)      :done, s1, 2026-11-03, 1d
    Rent a GPU, run the engine bake-off       :active, s2, 2026-11-05, 3d
    section Make the gate stronger
    Master-data validators                    :v1, 2026-11-05, 5d
    Grounding check                           :v2, after v1, 2d
    section Make accuracy provable
    Verify draft labels, grow the gold set    :g1, 2026-11-05, 14d
    Drift monitor + trust withdrawal          :d1, after v2, 4d
    section Any layout
    Template induction from one example       :t1, after d1, 10d
    Layout-varying synthetic data             :t2, after t1, 7d
```

1. **Today:** the work through `6877a9a` is committed; the engine-selection work in the tree is a separate piece to finish and commit on its own.
2. **This week:** (a) rent a GPU and run the engine bake-off so the reader decision rests on a table; (b) master-data validators and the grounding check, both cheap and both reduce wrong values *and* review volume; (c) start verifying labels and growing the gold set, because every later number depends on it.
3. **Next:** the drift monitor, then template induction from one example. That pair is what makes "any waybill, or part of one" a workflow rather than a model problem.
4. **Stop doing:** training a 7B adapter per layout; adding a layout to the app before it has a verified sample; reading accuracy off the 16 documents and calling it a rate.

### 10.5 What cannot be promised

- **1 error in 10,000 cannot be demonstrated on the data in hand.** It needs ≈ 30,000 individually checked values per field with none wrong, per template version, and the evidence is only as honest as the people producing it.
- **A layout the system has never seen will be refused or sent to review until someone confirms it once.** That is the intended behaviour; "any layout, flawlessly, with no one involved" is not something the literature or any vendor demonstrates.
- **Everything in §10.2 beyond the repository's own measurements is unverified here:** read the papers before relying on a figure.

### 10.6 Sources

[Baidu Unlimited-OCR](https://github.com/baidu/Unlimited-OCR) · [Perception, Layout, and Validation (2609.20110)](https://arxiv.org/pdf/2609.20110) · [Valid Per-Field Selective Risk Control (2608.14639)](https://arxiv.org/abs/2608.14639) · [Learn then Test (2110.01052)](https://arxiv.org/abs/2110.01052) · [Conformal prediction for KIE, IJDAR 2026](https://link.springer.com/10.1007/s10032-026-00572-y) · [Beyond Logprobs (2606.24420)](https://arxiv.org/pdf/2606.24420) · [SafePassage (2510.00276)](https://arxiv.org/abs/2510.00276) · [PaddleOCR-VL-1.6 (2606.03264)](https://arxiv.org/pdf/2606.03264) · [Best open-source OCR models, Roboflow](https://blog.roboflow.com/best-open-source-ocr-models/) · [VDInstruct (2507.09531)](https://arxiv.org/pdf/2507.09531) · [PPN (2307.10551)](https://arxiv.org/pdf/2307.10551) · [Information extraction design space (2502.18179)](https://arxiv.org/pdf/2502.18179) · [Selective labeling for visually rich documents](https://physics.paperswithcode.com/paper/radically-lower-data-labeling-costs-for) · [NAT (2404.00488)](https://arxiv.org/pdf/2404.00488) · [Online shift detection and conformal adaptation (2606.11949)](https://arxiv.org/pdf/2606.11949) · [Conformal test martingales (2602.13848)](https://arxiv.org/abs/2602.13848) · [KMM-CP (2603.26415)](https://arxiv.org/pdf/2603.26415) · [Calibrated Learning to Defer (2202.03673)](https://arxiv.org/pdf/2202.03673) · [Human-in-the-loop with two thresholds (2601.05974)](https://arxiv.org/abs/2601.05974)
