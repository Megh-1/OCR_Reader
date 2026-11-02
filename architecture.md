# IDP Pipeline: Architecture

> **What this document is.** The architecture of the IDP Pipeline repository as it exists on disk today, written from the code, `docker-compose.yml`, `app/config.py` and the Alembic migrations. Where something is *not* built (authentication, metrics, a cache), it says so instead of describing an aspiration. Measured numbers come from the comments in `app/config.py` and the project's `CLAUDE.md`; anything that is a proposal is labelled **Proposed**.
>
> **Diagrams** are [Mermaid](https://mermaid.js.org/). They render on GitHub, GitLab, VS Code (Markdown preview with the Mermaid extension), and most Markdown viewers.

**Status legend used throughout**

| Tag | Meaning |
|---|---|
| ✅ **Built** | In the code, covered by the test suite (512 tests at the last full run) |
| 🟡 **Uncommitted** | Built and tested, but sitting in the working tree, not yet in a commit |
| 🧪 **Experimental** | Exists as scripts or on the GPU box; **not wired into the application** |
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
8. [How the technology works right now: status and the experimental reader](#8-how-the-technology-works-right-now)
9. [Appendix: repository map, run commands, glossary](#9-appendix)
10. [Research survey and recommended direction](#10-research-survey-and-recommended-direction)

---

## 1. System overview & high-level design

### 1.1 What the system does

The IDP Pipeline turns piles of scanned or digital documents into structured JSON. A person uploads a sample page of a document type once and **marks it up in the browser**: they draw *anchor* boxes around printed labels that identify the form, and *field* boxes around the values to extract. That markup is saved as a versioned **template**. After that, a user uploads a batch of documents (PDF, JPEG or PNG). Each page is cleaned up, matched to a template by its anchors, and read field by field, either straight from the PDF's embedded text or with Tesseract OCR. Every value is then checked against rules the template author wrote (a regex, a check digit, arithmetic between fields) and given an **approval tier**: `auto` (safe to use without a human), `spot_check` (probably right, show it to a person), or `review` (rejected, a person must decide). Anything that is not `auto` lands in a **review queue** where a person confirms or corrects it next to a crop of the source page. The design principle that governs every choice is taken from the project's spec: **refuse and flag rather than emit a wrong value.**

### 1.2 Design philosophy and paradigm

| Aspect | Choice |
|---|---|
| **Paradigm** | **Modular monolith, two process types.** One Python codebase (`app/`) runs as an API server (FastAPI) and as N batch workers (RQ). Same code, same image, different entry command. |
| **Extraction approach** | **Template-driven, deterministic, no learned model in the shipped path.** A person declares where the values are; nothing is inferred. Tesseract and the PDF text layer are the only readers. |
| **Failure philosophy** | Every uncertain outcome becomes a *queue entry for a human*, never a guess. A check that cannot decide is `skipped`, never `passed`. Every safeguard only ever adds caution. |
| **State** | PostgreSQL is the system of record for everything durable (templates, results, review queue, batch state, even the uploaded bytes while a document is in flight). Redis holds only the job queue. |
| **Frontend** | A single-page console (React) served as static files and talking to the API through a same-origin reverse proxy. |
| **Templates** | **Append-only**: a save always inserts a new version, never edits one. A past extraction stays reproducible against the exact version that ran. |

### 1.3 System context

```mermaid
flowchart LR
    user(["👤 Operator<br/>uploads, reviews,<br/>marks up templates"])
    sys{{"<b>IDP Pipeline</b><br/>template-driven<br/>document extraction"}}
    consumer(["⚙️ Downstream system<br/>consumes JSON via<br/>GET /results"])
    docs[("📄 Documents<br/>PDF / JPEG / PNG<br/>≤ 15 MB, ≤ 20 pages")]

    user -- "marks templates,<br/>uploads batches,<br/>resolves flagged fields" --> sys
    docs -- "uploaded" --> sys
    sys -- "extracted fields<br/>+ approval tier<br/>+ confidence" --> consumer
    sys -- "review & spot-check<br/>queue" --> user

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

    browser -->|"HTTP :8080"| fe
    fe -->|"/templates /review /results /documents<br/>proxy_pass → app:8000"| app
    app -->|"SQLAlchemy + psycopg"| pg
    app -->|"enqueue job"| rd
    wk -->|"dequeue job"| rd
    wk -->|"SQLAlchemy + psycopg"| pg
    app -.->|"subprocess (sync endpoint)"| tess
    wk -.->|"subprocess × 8 threads"| tess

    classDef store fill:#eef3ff,stroke:#1f4fd8
    class pg,rd store
```

Key facts that shape this topology:

- **The API and the worker share nothing but Postgres and Redis.** Uploaded bytes are stored *on the database row*, not on a shared disk, so any worker on any machine can process any document.
- **The app container migrates the database on start** (`CMD ["sh","-c","python -m alembic upgrade head && uvicorn ..."]`); the worker waits for the app's health check for that reason.
- **Every field read is its own Tesseract process**, so the number of OS processes per document is large; see §6 for the OpenMP cap that keeps that from thrashing.
- **Two reverse-proxy copies must stay in sync.** `frontend/vite.config.js` (dev) and `frontend/nginx.conf` (container) both list the route prefixes the UI calls. A new backend prefix needs an entry in *both*.

### 2.2 Backend modules (numbered after the spec)

The nine modules from the reference PDF are plain Python functions that call each other's *public* functions; none re-implements another.

```mermaid
flowchart LR
    subgraph API["FastAPI app  (app/main.py: 7 routers)"]
        M1["<b>1 Ingestion</b><br/>ingestion.py<br/>MIME sniff, size & page caps,<br/>PDF → 200 DPI pages,<br/>text-layer extraction"]
        M2["<b>2 Preprocessing</b><br/>preprocessing.py<br/>page detect, deskew,<br/>CLAHE, binarize, rule removal"]
        M5["<b>5 Type identification</b><br/>type_identification.py<br/>anchor OCR vs every template"]
        AL["<b>Alignment</b> 🟡<br/>alignment.py<br/>landmark words → shift+stretch"]
        M6["<b>6 Field extraction</b><br/>field_extraction.py<br/>text-layer OR Tesseract"]
        M7["<b>7 Validation</b><br/>validation.py + checks.py<br/>regex · checksum · confidence<br/>· cross-field checks → tier"]
        M8["<b>8 Output compiler</b><br/>output_compiler.py<br/>JSON result, persists,<br/>owns /results*"]
        M9["<b>9 Review queue</b><br/>review_queue.py<br/>field_review · spot_check<br/>· new_template_needed"]
        RG["<b>Review groups</b> 🟡<br/>review_groups.py<br/>groups · bulk accept<br/>· audit sample · evidence"]
        TR["<b>Trust</b> 🟡<br/>trust.py<br/>audited auto-approval"]
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
| Review groups 🟡 | `review_groups.py` | Group pending entries (same value across documents, or per document); **bulk accept**; the evidence report | The **server** decides what may be bulk-accepted: only a spot check with a value, never a Module 7 rejection; every bulk accept holds back a random audit sample |
| Trust 🟡 | `trust.py`, `utils/stats.py` | Per (template, **version**, field): spot checks a person resolved one at a time, how many they changed, the exact Clopper-Pearson upper bound; a field under the target skips the spot-check queue | Bulk accepts are not evidence; one wrong value found raises the bound and withdraws the trust |
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
    G -- no --> H2["Pass 2 preprocess with the template's hints<br/>(page_aspect_ratio, ruled_form)<br/>re-fit alignment if the frame changed"]
    H2 --> H3["Module 6b: Tesseract per field,<br/>PSM 4/6/7 (+ digit whitelist),<br/>8 fields in parallel,<br/>on the <i>grayscale</i> rendering"]
    H1 --> I
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

**Audited auto-approval (🟡, §2.7)** adds one more way to reach `auto`: a `spot_check` on a field a person has checked enough of is approved as `auto` with `auto_basis = trusted_field`, bar a random 5% that still go to a person. It only ever applies to `below_auto_threshold` and `no_validator` spot checks that have a value; a Module 7 rejection or a blank optional is never promoted.

Why the "no validator ⇒ never auto" rule exists: on the synthetic corpus **every** wrong value Module 7 ever accepted sat in a field with no regex or checksum, at confidence 64–87, only 3–5 points below the 0.90 threshold. The rule closes that gap at a measured cost of 186 correct reads held back as spot-checks.

### 2.5 Frontend architecture

React 19 + Vite 8 + Tailwind v4, shadcn-style components, Fabric.js for the template canvas. There is **no frontend test runner**; the build is checked by `oxlint` and by looking at it in a browser.

```mermaid
flowchart TB
    subgraph SPA["frontend/src  (hash-routed single-page app)"]
        APP["<b>App.jsx</b><br/>nav · review-queue badge ·<br/>tier legend · hash router"]
        subgraph screens["screens/"]
            S1["<b>BatchMonitor</b><br/>#/batch (default)<br/>upload + 1 s status polling"]
            S2["<b>Results</b><br/>#/results<br/>table, in-place correction,<br/>JSON download"]
            S3["<b>ReviewQueue</b><br/>#/review<br/>crop + Accept/Correct bar<br/>keys A C J K"]
            S4["<b>TemplateDesigner</b><br/>#/templates<br/>Fabric.js canvas:<br/>anchors, fields, checks"]
        end
        subgraph shared["shared"]
            API["<b>api.js</b><br/>fetch wrapper,<br/>ApiValidationError,<br/>error-line formatter"]
            COMP["components/<br/>FieldCrop · ConfidenceGauge ·<br/>TierBadge · StatusPill · ui/*"]
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
| Upload (`BatchMonitor`) | `submitBatch`, `getBatchStatus`, `listBatches`, `listTemplates` |
| Results | `listResults`, `getExtractionResult`, `correctResultField`, `getTemplateVersion` |
| Review | `listReviewQueue`, `resolveFieldReview`, `resolveNewTemplateNeeded`, `getExtractionResult`, `getTemplateVersion`, `resultImageUrl` |
| Templates | `createTemplate`, `getTemplate`, `listTemplates`, `listTemplateVersions`, `getTemplateVersion`, `listResults`, `resultImageUrl` |

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
        W->>T: read fields (subprocess × 8)
        W->>P: INSERT extraction_results, review_queue rows
        W->>P: save progress to batch_documents.result
    end
    W->>P: status=completed, payload=NULL (upload dropped)

    U->>A: GET /review, PATCH /review/{id}
    A->>P: resolve entry, clear needs_review on the result
```

### 2.7 Keeping the review queue short without losing accuracy 🟡

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

**What this does and does not do.** It cannot certify 1 error in 10,000 from a few hundred documents: that needs **29,956 checked values with none wrong, per field** (so a fresh install trusts nothing). It makes the evidence accrue as a by-product of normal review, and it gives the queue a short path (grouping, bulk) in the meantime. The 5% audit rate and the 30 s cache are starting figures, not measurements.

### 2.8 Telling templates apart: whole forms, parts of forms, and anchor subsets 🟡

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
- **`hints.min_anchors_matching`** (new): require N of M anchors instead of all, with 2 ≤ N ≤ M. It lets one smudged or cut-off label not refuse a page. It never lowers the bar below two matching labels, and field boxes that were drawn on a whole page still sit where they sat on the whole page, so a half-page scan needs boxes and anchors drawn on a half-page sample.
- **The batch status carries a "where the pages went" summary** (`summary.by_template`, `summary.unmatched`), so a batch that spread over several templates, or lost pages to `no_match`, says so.
- **The rule editor** (Templates > Checks): lists a template's cross-field rules with a *Test* button (dry run over already-processed documents), proposes rules that held on every readable document (`suggest_checks`, at least two documents, non-degenerate), and writes new `product`/`sum`/`equal`/`one_of` rules. A suggestion is only a proposal until a person presses Add.

---

## 3. Data architecture & schema strategy

### 3.1 Primary store

**PostgreSQL 17**, accessed with SQLAlchemy 2.x (`psycopg` v3), migrated with Alembic (six migrations, `0001`–`0006`). The strategy is **relational spine + JSONB for shape-varying documents**: ids, versions, statuses and timestamps are real columns; templates, field results, queue payloads and batch results are JSONB because their shape belongs to the template/entry type, not to the schema.

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
        jsonb fields "field_id → value, confidence, tier, reasons"
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
    }
    Template "1" *-- "1..*" TemplateAnchor
    Template "1" *-- "1..*" TemplateField
    Template "1" *-- "0..*" TemplateLandmark
    Template "1" *-- "0..*" TemplateCheck
    Template "1" *-- "1" TemplateHints
```

Coordinates are **page-relative fractions**, not pixels, and are re-validated server-side (`0 ≤ x1 < x2 ≤ 1`) regardless of what the designer UI sent. `min_anchors_matching` (2 ≤ N ≤ anchor count) relaxes "all anchors" to "N of them". `is_part` marks a template drawn on part of a form; saved for automatic detection it needs two anchors. `hint_only` removes a template from the no-hint scan (otherwise a partial template over a layout another template already covers makes every un-hinted upload `ambiguous_match`).

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
| `POST /templates/suggest-checks` | 3 | Propose cross-field rules that held on every readable processed document of a template | 200 `{documents, suggestions[]}` | |
| `POST /templates/test-check` | 3 | Dry-run one rule: passed / failed / skipped counts and up to 5 failures | 200 | 422 |
| `GET /results?limit=&template_id=` | 8 | Result summaries | 200 | |
| `GET /results/{id}` | 8 | One `CompiledResult` (the JSON deliverable) | 200 | 404 |
| `PATCH /results/{id}/fields/{field_id}` | 8 | Correct a value in place | 200 `FieldResult` | 404 |
| `GET /results/{id}/image` | 8 | Review JPEG of the page | 200 `image/jpeg` | 404 |
| `GET /review?status=&entry_type=` | 9 | List queue entries (default `pending`) | 200 | |
| `PATCH /review/{id}` | 9 | Resolve: `confirmed_value` or `created_template_id` | 200 | 404, 422 (missing the field for that entry type) |
| `GET /review/groups?by=value or document, &template_id=, &limit=, &offset=` | 9+ | Pending value entries grouped (same value across documents, or per document) | 200 `{by, total, groups[]}` | 422 (bad `by`) |
| `POST /review/bulk` | 9+ | Accept many spot checks; server-enforced eligibility; random audit sample held back | 200 `{accepted[], held_for_audit[]}` | **422** `{refused: {id: reason}}` (nothing applied) |
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
      "reasons": []                          // e.g. ["format_invalid", "check_digit_invalid",
    },                                       //       "cross_check_failed:<label>", "low_confidence"]
    "gross_weight": { "...": "..." }
  }
}
```

A consumer should treat only `approval_tier == "auto"` (or a field a human has resolved) as usable without review.

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
- **Windows workers** fall back to `SimpleWorker` (no fork), so a hung job is only cut off by a timer thread. Use the Linux image for production.

### 6.4 Logging, monitoring and error tracking

| Capability | State |
|---|---|
| **Application logging** | ✅ Python `logging`, logger `app`, INFO level, to stderr (`runtime.configure_logging`). Per page it logs the deskew angle, method and whether it was applied, and the alignment summary (landmarks matched, scale, shift, rms). Uvicorn's own loggers are separate |
| **Per-document audit** | ✅ `batch_documents` keeps `status`, `attempts`, `error`, `started_at`, `finished_at`; `extraction_results` keeps `template_id` + `template_version` + `extracted_at` |
| **Metrics** (Prometheus/StatsD) | ⛔ None |
| **Distributed tracing** | ⛔ None |
| **Error tracking** (Sentry etc.) | ⛔ None; exceptions land in container logs only |
| **Health check** | ✅ `GET /docs` for the app container; `pg_isready` and `redis-cli ping` for the stores. No dedicated `/health` or `/ready` endpoint |
| **Quality monitoring** | ✅ Offline scripts: `evaluate_accuracy.py`, `evaluate_batch.py` (through a running API), `evaluate_deskew.py`, `evaluate_forms.py` |

**Proposed** minimum: a `/healthz` + `/readyz` pair (DB + Redis checks), `prometheus-fastapi-instrumentator` plus RQ queue-depth and per-tier counters (auto / spot_check / review rate is the single most useful business metric), Sentry for exceptions, and structured JSON logs carrying `batch_id`, `document_id` and `template_id` on every line.

---

## 7. Architecture decision records (ADRs) / trade-offs

| # | Decision | Chosen | Over | Why | Cost / risk accepted |
|---|---|---|---|---|---|
| 1 | Extraction approach | **User-marked coordinate templates** with Tesseract / PDF text | ML layout models, LLM-only extraction | Predictable, auditable, no training data needed, every miss is explainable; matches "refuse and flag" | Brittle when a form drifts: needed alignment (ADR 5) and still needs one template per printed layout |
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
| 21 | Experimental reader gate | **Auto only if (two readers agree OR an arithmetic check) AND format passes** | Trusting the VLM's answer | A prompt that contained a valid example number was copied by the model on clipped crops (found and removed); agreement is what survives that | Every field costs a second read |


---

## 8. How the technology works right now

### 8.1 What is shipped versus experimental

```mermaid
flowchart TB
    subgraph shipped["✅ SHIPPED PATH (the application)"]
        direction LR
        A1["Ingestion"] --> A2["Preprocess<br/>2-pass"] --> A3["Anchors<br/>+ 🟡 alignment"] --> A4["Template boxes →<br/>text layer or Tesseract"] --> A5["Validate<br/>+ tiers"] --> A6["Results + Review queue"]
    end
    subgraph exp["🧪 EXPERIMENT (scripts + GPU box; NOT in the app)"]
        direction LR
        B1["Template-guided crops<br/>3 widening levels<br/>0 / 0.02 / 0.04"] --> B2["Qwen2.5-VL-7B<br/>+ LoRA fine-tune"] --> B3["Staged read:<br/>first window whose answer<br/>passes the format wins"] --> B4["Gate: auto only if<br/>(2 readers agree OR arithmetic check)<br/>AND format passes"]
    end
    shipped -. "same templates,<br/>same validation rules" .- exp
```

### 8.2 Committed versus uncommitted

Latest commit: `698ccf4` *Add page alignment so templates survive A4 pages, scan margins and other printers*. The working tree additionally holds (🟡 tested, 512 tests pass, lint 0 errors at the last full run, **not committed**): the review grouping / bulk accept / evidence / audited auto-approval stack (`review_groups.py`, `trust.py`, `utils/stats.py`), part templates, most-specific-wins matching, `min_anchors_matching`, the rule editor and its two endpoints, the batch "where the pages went" summary, alignment refinements and config tunables, **field read options** (`from_field` / `extract_pattern` / `squash_spaces` / `optional`), the `blank_optional` spot-check reason, the Bangkok and other waybill forms (`scripts/waybill_forms.py`), the designer carrying landmarks/hints/checks through load-and-save, and the new landmark JSONs. 🧪 The experiment scripts (`scripts/gpu/*`, `make_train_set.py`, `score_*.py`, `proto_*.py`, …) are also untracked.

### 8.3 What the 16 real waybills showed

Template pipeline on the real documents, fields right of 33 per document, using landmark lists built from **training pages only** so no held-out page aligns itself:

| Layout | Document | Kind | Template pipeline | Page-OCR word reader (experiment) |
|---|---|---|---|---|
| Bangkok (train) | 08, 11 | text layer | **33 / 33** each | n/a |
| Bangkok (train) | 02, 06, 12 | scan | 8, 5, 6 | 12, 8, 11 |
| Bangkok (held out) | 01 | scan | 7 (**1 wrong value auto-approved**) | 13 |
| Bangkok (held out) | 03, 13 | scan | **refused** (anchor 43 / 54) | 13, 6 |
| Bangkok (held out) | 14 | scan | 1 | 1 |
| Mumbai | 09 / 10 (train) | text / scan | 31, 11 | 25 across both |
| Mumbai | 07 (held out) | text layer | 10 | 6 |

Reading it plainly: **clean text-layer PDFs are solved; scans of the same form are not**, because the drift is larger than the boxes tolerate. The one wrong auto-approved value on document 01 is exactly the failure the approval rule is meant to stop, and it is why the experimental reader's gate requires agreement or an arithmetic check.

### 8.4 State of the experimental reader

- **Done:** frozen train/test split (`data/real_awb/split.json`); synthetic training data with exact ground truth and *visibility-aware answers* (a crop that does not contain the value is labelled EMPTY, which fixed a hallucination where the model wrote `123,456.00` into crops showing only captions); **noisy synthetic data** (`--hard`: heavy skew, speckle, JPEG 45–70, uneven contrast) so the reader sees scans, not renders; first-round adapters for Bangkok and Mumbai (v2) and their CPU baselines.
- **Found and fixed:** the prompt for the AWB field contained a *valid mod-7 example number*, and on clipped crops the model **copied it**. All example numbers were removed from the prompts and the training sets rebuilt. This is the reason the gate needs a second reader or an arithmetic check: a value that is well-formed and checks out can still be invented.
- **Running now (GPU box `10.20.37.130`, shared, never touched beyond this project's directory):** one **mixed adapter** `wb_v3` (Bangkok + Mumbai, plain + hard noise + the real *train* pages repeated 3×, LoRA r = 16, 2 epochs, 1,625 steps at ≈ 6.9 s/step). A chain script scores epoch 1 (as soon as it exists, on whichever GPU has room) and epoch 2 plus the untuned model after training, on the synthetic validation sets (plain and hard) and the real train pages. **Results are not in this document yet**; they will be added when scoring ends.
- **Protocol:** choose epoch 1 vs 2 on the *synthetic validation set only*; read the held-out real documents **once**, after the choice is locked.
- **Not done:** integrating any VLM reader into `app/`; a reader that works on a **layout it has never seen** (the central open problem, see §10); layouts with no template or synthetic generator (SalamAir, Pioneer); fine-tuning Tesseract itself.
- **Running it locally:** the 7B reader needs a ≈ 16 GB GPU in bf16; this laptop has an Intel iGPU and 15.7 GB RAM. §10 proposes a ≈ 1B specialist model that fits.
- **Throughput caveat:** ≈ 0.2 s per crop batched ⇒ ≈ 5 s per scanned document per GPU if every field is sent, so the VLM would be called only for fields that need it.

### 8.5 Known gaps and next steps (priority order)

1. **Authentication/authorization and TLS** (§5.1): required before any shared deployment.
2. **Commit** the tested uncommitted work (waiting on your go-ahead, as agreed). It is now a large diff: commit in slices (review stack, templates/detection, alignment/forms, experiment scripts).
3. **Verify the draft labels** in `data/real_awb/labels/` (they are unverified; every accuracy figure on real documents depends on them).
4. **Finish the reader evaluation** (stage 1 is running); then decide, with §10's benchmark, whether the second reader is the 7B adapter or a small specialist model, and integrate it behind the same gate.
5. **Auto-build landmarks at template-save time** in the designer (today `scripts/build_landmarks.py` is run by hand over other real documents).
6. **Observability**: `/healthz`, metrics, error tracking, structured logs (§6.4).
7. **Move blobs out of Postgres** and **split migrations from API start-up** before scaling out.
8. **More real data per layout**: a handful of test documents cannot certify 1-in-10,000; the project needs hundreds of labelled real pages per layout and a gate evaluated per auto-approved value.

---

## 9. Appendix

### 9.1 Repository map

```mermaid
flowchart LR
    root["idp-pipeline/"] --> app["app/<br/>FastAPI + pipeline"]
    root --> fe["frontend/<br/>React console"]
    root --> alembic["alembic/versions/<br/>0001–0006"]
    root --> scripts["scripts/<br/>seed, evaluate, build corpora,<br/>waybill_forms, 🧪 gpu/"]
    root --> tests["tests/<br/>512 tests, real-OCR tests,<br/>fixtures/ git-ignored"]
    root --> docker["Dockerfile · docker-compose*.yml<br/>docker/postgres-init/"]
    root --> docs["CLAUDE.md · ACCURACY_REPORT.md<br/>idp-phase1-master-reference.pdf<br/>architecture.md"]
    app --> modules["modules/ (the 9 modules + batch + alignment + checks + pipeline)"]
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
| 🧪 Experiment | Qwen2.5-VL-7B-Instruct, PEFT LoRA, shared 2 × 32 GB GPU box over SSH |

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
4. **Benchmark a ≈ 1B specialist reader** (PaddleOCR-VL-1.6, GLM-OCR) on the 16 real waybills, cell by cell, against Tesseract and the 7B adapter. If it matches the 7B on crops, you get a reader that runs on this laptop and in Docker without a GPU server. Done = a table of per-field accuracy and wrong-confident counts for each reader.
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
    Stage 1 results (running)                 :active, s1, 2026-10-30, 1d
    Benchmark a small specialist reader       :s2, after s1, 3d
    section Make the gate stronger
    Master-data validators                    :v1, 2026-11-02, 5d
    Grounding check                           :v2, after v1, 2d
    section Make accuracy provable
    Verify draft labels, grow the gold set    :g1, 2026-11-02, 14d
    Drift monitor + trust withdrawal          :d1, after v2, 4d
    section Any layout
    Template induction from one example       :t1, after d1, 10d
    Layout-varying synthetic data             :t2, after t1, 7d
```

1. **Today:** wait for stage 1; commit the tested work in slices (it is a large uncommitted diff, and losing it would cost more than anything below).
2. **This week:** (a) master-data validators and the grounding check, both cheap and both reduce wrong values *and* review volume; (b) start verifying labels and growing the gold set, because every later number depends on it; (c) run the small-reader benchmark so the reader decision rests on a table.
3. **Next:** the drift monitor, then template induction from one example. That pair is what makes "any waybill, or part of one" a workflow rather than a model problem.
4. **Stop doing:** training a 7B adapter per layout; adding a layout to the app before it has a verified sample; reading accuracy off the 16 documents and calling it a rate.

### 10.5 What cannot be promised

- **1 error in 10,000 cannot be demonstrated on the data in hand.** It needs ≈ 30,000 individually checked values per field with none wrong, per template version, and the evidence is only as honest as the people producing it.
- **A layout the system has never seen will be refused or sent to review until someone confirms it once.** That is the intended behaviour; "any layout, flawlessly, with no one involved" is not something the literature or any vendor demonstrates.
- **Everything in §10.2 beyond the repository's own measurements is unverified here:** read the papers before relying on a figure.

### 10.6 Sources

[Perception, Layout, and Validation (2609.20110)](https://arxiv.org/pdf/2609.20110) · [Valid Per-Field Selective Risk Control (2608.14639)](https://arxiv.org/abs/2608.14639) · [Learn then Test (2110.01052)](https://arxiv.org/abs/2110.01052) · [Conformal prediction for KIE, IJDAR 2026](https://link.springer.com/10.1007/s10032-026-00572-y) · [Beyond Logprobs (2606.24420)](https://arxiv.org/pdf/2606.24420) · [SafePassage (2510.00276)](https://arxiv.org/abs/2510.00276) · [PaddleOCR-VL-1.6 (2606.03264)](https://arxiv.org/pdf/2606.03264) · [Best open-source OCR models, Roboflow](https://blog.roboflow.com/best-open-source-ocr-models/) · [VDInstruct (2507.09531)](https://arxiv.org/pdf/2507.09531) · [PPN (2307.10551)](https://arxiv.org/pdf/2307.10551) · [Information extraction design space (2502.18179)](https://arxiv.org/pdf/2502.18179) · [Selective labeling for visually rich documents](https://physics.paperswithcode.com/paper/radically-lower-data-labeling-costs-for) · [NAT (2404.00488)](https://arxiv.org/pdf/2404.00488) · [Online shift detection and conformal adaptation (2606.11949)](https://arxiv.org/pdf/2606.11949) · [Conformal test martingales (2602.13848)](https://arxiv.org/abs/2602.13848) · [KMM-CP (2603.26415)](https://arxiv.org/pdf/2603.26415) · [Calibrated Learning to Defer (2202.03673)](https://arxiv.org/pdf/2202.03673) · [Human-in-the-loop with two thresholds (2601.05974)](https://arxiv.org/abs/2601.05974)
