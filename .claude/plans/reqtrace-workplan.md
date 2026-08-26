# ReqTrace — Requirements-to-Code Traceability Chatbot

## Context

Sprint 2 course project (`125.md`): a domain-specialised chatbot with advanced RAG (query translation + structured retrieval), ≥3 tool calls, and a polished UI. Budget ~20–25h. Bonus bar: ≥2 medium + 1 hard optional tasks.

**Domain (user's own field — automotive embedded SW):** a chatbot over AUTOSAR software specifications and a permitted C code repository. It answers requirement questions with source links and a split-window preview, checks code for implementation evidence, and generates a full requirements-to-code traceability report.

**Corpus (real, not synthesized — decided during brainstorming):**
- Requirements: AUTOSAR SWS **CAN Driver** + **CAN Interface** PDFs, fetched directly from autosar.org (no registration), e.g. `https://www.autosar.org/fileadmin/standards/R23-11/CP/AUTOSAR_CP_SWS_CANDriver.pdf`. Requirement IDs `[SWS_Can_00011]`, each citing upstream `SRS_*` IDs → two-level traceability SRS → SWS → code.
- Code: `github.com/openAUTOSAR/classic-platform` (Arctic Core, Apache-style OSS, C), scoped to `communication/` (`Can`, `CanIf`, `PduR`, `Com`). Code carries `/** @req ... */` annotations. Code is older than R23-11 specs → real gaps/drift for the report to surface (a feature, not a bug — say so in README).
- **Corpus is abstracted**: everything downstream depends only on `Requirement` and `CodeUnit` records + a per-project `project.yaml` manifest (doc globs, ID regexes, repo URL + pinned SHA, language, include/exclude globs). Swapping corpora later = new manifest + possibly one loader function. NOT a plugin framework — a dict of loader functions is the ceiling of abstraction.
- Do NOT commit the PDFs or the cloned repo; ingestion fetches them (`data/` is gitignored).

**Architecture (Approach C, approved):** tool-calling LangChain agent for conversation; deterministic, testable engines behind the tools. LLM + embeddings + rerank ALL via OpenRouter (course requirement; OpenRouter now has `/api/v1/embeddings` — no local torch/sentence-transformers anywhere).

**Bonus tasks designed in (all four approved):** hybrid search (hard), RAGAS eval (hard, sequenced LAST as the slippable item), prompt-injection protection (medium), token/cost display (medium), source citations (easy).

## Decisions from grill session (2026-08-26, all user-approved)

- **Models (in project.yaml):** chat = `anthropic/claude-sonnet-4.5`; judge/rerank = `openai/gpt-4o-mini`-class; embeddings = `openai/text-embedding-3-small`. Cost target ≤$0.50/scoped report run; hard stop `MAX_REPORT_COST_USD` (default $2).
- **SRS docs:** cite-only (not ingested); matrix UI renders SRS IDs as tooltipped chips ("upstream requirement, not ingested").
- **Spec release:** R23-11. Fallback to an older release only if gap rate >80%.
- **Threads (UX bar raised — "very good app"):** conversations persisted in SQLite. Each message stores its FULL SSE event stream (tool chips, citations, usage replay exactly on reload). Thread sidebar; LLM auto-titles (cheap model) with rename-on-click. Thread export as Markdown + JSON (banks the 2nd easy bonus).
- **Checkpoints:** timeline rail beside thread, one dot per user request (icon = action type: search/lookup/evidence/report). Click → scroll to exchange AND restore source pane to that moment's state (pure replay of stored events). NO rollback/branching (schema shouldn't preclude it later).
- **Deployment:** local-only; first-run setup screen detects missing index/API key and walks through ingestion with progress (no stack traces).
- **Styling:** Tailwind + shadcn/ui (drawer, tabs, resizable panes, data table, toasts, skeletons).
- **Out-of-domain:** polite scoped refusal + redirect, system-prompt enforced, 2 test fixtures.
- **Extraction:** deterministic-only + visible extraction_report.md; LLM fallback only if golden recall <90%.
- **Repo:** this repo is the submission monorepo; frontend/ + backend/ at root; main branch; conventional commits; data/ gitignored.
- **Design-first:** before Phase 4, produce a /design canvas (main screen w/ sidebar+chat+split pane, PDF vs code states, report flow, first-run/empty states); Phase 4 implements to the approved mockup.
- **Budget re-estimate:** ~28h. RAGAS remains the slippable tail.

## Stack

- **Frontend:** Next.js (App Router, TS). `next.config` `rewrites()`: `/api/py/:path*` → `http://localhost:8000/:path*` (no CORS).
- **Backend:** Python 3.12, FastAPI + uvicorn, LangChain v1, `openai` SDK pointed at OpenRouter (`base_url=https://openrouter.ai/api/v1`).
- **Storage:** SQLite (registry: requirements, code units, report runs, embedding cache) + Chroma (vectors, collection per `project_id`) + `rank_bm25` (in-memory at startup from SQLite).
- **Parsing:** PyMuPDF (PDF text + page + bbox/char-span), `tree-sitter` + `tree-sitter-c` (C functions with line spans), regex from manifest for req IDs.
- **Models (in `.env` / manifest, all OpenRouter):** chat = a strong tool-calling model; judge/rerank = a cheap fast model; embeddings = `text-embedding-3-small` (PIN the model ID in manifest; cache embeddings by content-hash in SQLite).

## Repo layout

```
frontend/                     # Next.js app
backend/
  ingestion/    run.py, pdf_loader.py, req_extractor.py, code_indexer.py
  core/         models.py (Requirement, CodeUnit — pydantic), manifest.py, db.py
  retrieval/    hybrid.py (BM25+dense+RRF), translate.py (multi-query), 
                self_query.py (typed filters), rerank.py (LLM listwise)
  engines/      evidence.py (3-tier check), report.py (batch job + SSE progress)
  agent/        agent.py, tools.py (5 tools), prompts.py
  api/          main.py, chat.py (SSE), documents.py, reports.py, security.py
  tests/        fixtures/golden/, test_*.py, injection_set/
projects/autosar-can/project.yaml
docs/specs/2026-08-26-reqtrace-design.md   # write the approved design here first
```

## Core data model

```
Requirement: id, title, text, section_path, page, bbox/char_span,
             source_doc, upstream_ids (SRS_*), named_symbols (e.g. Can_Write),
             version, project_id
CodeUnit:    repo_path, language, symbol, kind, line_span, text,
             req_annotations (from /** @req */), git_sha, project_id
```

## The 5 tools (agent = LangChain v1 tool-calling loop)

1. `lookup_requirement(req_id)` — normalize ID → SQLite exact hit → requirement + citation payload `{req_id, doc, page, bbox}`. Never semantic.
2. `search_requirements(query, filters?)` — the advanced-RAG pipeline as ONE tool:
   multi-query expansion (3 LLM rewrites, RAG-Fusion) → self-query typed filter extraction (Pydantic: module, section, doc_type) → BM25 + dense in parallel → RRF fusion → LLM listwise rerank top-20 → top-5. Each stage a pure function (RAGAS evaluates this).
3. `search_code(query | symbol)` — AST-chunk retrieval over the pinned repo snapshot only.
4. `check_implementation(req_id)` — evidence engine, 3 tiers:
   T1 annotation scan (`@req` refs; *claimed* evidence, still verified);
   T2 anchor retrieval (named_symbols → AST index) + hybrid semantic, ≤8 candidates;
   T3 tool-less LLM judge → `{status: implemented|partial|missing|unverifiable, evidence:[{file,lines,rationale}], confidence}` (Pydantic-validated; `unverifiable` allowed).
5. `generate_traceability_report(scope)` — launches batch job (FastAPI BackgroundTask + in-process job registry, NO Celery): iterate requirements in scope through `check_implementation`, cache verdicts by `(req_id, git_sha, model_id)`, stream `{done,total,current}` via SSE. Output matrix SRS→SWS→verdict→evidence, coverage stats, export MD/CSV/JSON. Show cost estimate BEFORE launch.

## API (FastAPI)

- `POST /chat` → SSE, envelope `{type, data}` with types: `token`, `tool_start`, `tool_result`, `citation`, `usage` (tokens+$ per message), `done`, `error`.
- `GET /requirements/{req_id}`; `GET /documents/{doc}/view?highlight=req_id` (page + bbox for pdf.js overlay); `GET /code/{path}?lines=a-b` (allowlist-resolved).
- `POST /reports` → `{job_id, est_cost}`; `GET /reports/{id}/events` (SSE); `GET /reports/{id}`; `GET /reports/{id}/export?fmt=md|csv|json`.
- Threads: `POST /threads`; `GET /threads`; `GET /threads/{id}` (with events); `PATCH /threads/{id}` (rename); `DELETE /threads/{id}`; `GET /threads/{id}/export?fmt=md|json`.
- Setup: `GET /setup/status` (index present? key valid?); `POST /setup/ingest` → job; `GET /setup/ingest/events` (SSE progress).

## Frontend (single page, 3 zones)

- **Chat pane:** streamed tokens; tool-call chips with live status; structured citation chips `[SWS_Can_00011]` (from SSE `citation` events, never parsed from prose) — click opens source pane; per-message token/cost line.
- **Source pane (split window):** Tab A = real PDF page via **pdf.js** with highlight overlay from stored bbox; Tab B = code file via Shiki, scrolled to evidence line-span.
- **Report drawer:** launch w/ scope + cost estimate → progress bar (SSE) → filterable matrix table; row click opens both tabs. Export buttons.
- Plain React state + one SSE hook. No state library.

## Security (prompt-injection = bonus medium, designed-in)

- All retrieved text (docs AND code) enters prompts fenced + framed as data-not-instructions.
- Capability separation: only the chat agent has tools; judge + reranker are tool-less structured calls (injection can corrupt one verdict, never trigger an action).
- Manifest pins repo URL + SHA; file serving resolves against indexed snapshot only; reject path traversal.
- Input validation (ID regex, length caps), in-memory token-bucket rate limit on SSE endpoints.
- `tests/injection_set/`: ~10 adversarial fixtures (poisoned code comment, poisoned doc chunk, system-prompt extraction, tool-forcing) asserted schema-bound in pytest.

## Testing

- Golden fixtures: frozen PDF pages + hand-labeled expected requirements (corpus-swap insurance); known C files → expected CodeUnits; RRF math; ID-regex edges.
- Engine tests with recorded/mocked LLM replies; cache invalidation by SHA/model.
- E2E smoke script: boot both servers, 3 canned questions, one scoped report to completion.
- **RAGAS (last):** ~25 golden Q&A from the SWS docs; metrics: faithfulness, answer relevancy, context precision/recall; report **naive top-k baseline vs full pipeline** table in README.

## Work-package breakdown (~32h total; WP7.2 RAGAS is the slippable tail)

Execution order: WP1 → WP2 → WP3 → WP4 → WP5 (F5.0 design canvas may run right after WP1) → WP6 → WP7.

*(Amended 2026-08-26 during WP1, after measuring the real corpus: added F4.4,
the judge evaluation against the `@req`/`!req` annotation ground truth, ~1h.
The corpus also grew from 2 documents to 4 — CAN Driver, CAN Interface, CAN
Transport Layer, CAN State Manager, 1054 requirements — and the code scope
dropped `Can`, which has no implementation in the permitted repository.
Everything measured is in `docs/findings/2026-08-26-corpus-and-toolchain-findings.md`.)* WP3 ships the agent with the 3 retrieval tools; the 2 evidence/report tools register in WP4 (S4.3.2). Each story lists its deliverable and acceptance criterion.
*(Audited 2026-08-26 by coverage-review agent: 2 gaps + 3 partials found and fixed — read endpoints S3.3.2, setup backend F3.6, error-handling S3.3.3/S5.2.5, context chunks S1.3.5, cache-invalidation acceptance S4.2.1.)*

### WP1 — Platform & Corpus Foundation (~6h)

**F1.1 Project scaffold & dev environment**
- S1.1.1 Backend scaffold: uv project, Python 3.12 pin, FastAPI skeleton w/ `/health`, pytest+ruff. *Accept: `uv run pytest` green, `/health` responds.*
- S1.1.2 Frontend scaffold: create-next-app (TS, App Router), Tailwind, shadcn/ui init. *Accept: dev server renders shell page.*
- S1.1.3 Wiring: `rewrites()` proxy `/api/py/:path*`→`:8000`, `.env`/`.env.example` (OPENROUTER_API_KEY, MAX_REPORT_COST_USD), `Makefile` `dev` target boots both. *Accept: frontend fetches `/api/py/health`.*
- S1.1.4 Repo hygiene: `.gitignore` (`data/`, `.env`, caches), README stub, spec committed. *Accept: `git status` clean after ingestion run.*

**F1.2 Core models & storage**
- S1.2.1 Pydantic `Requirement`/`CodeUnit` (per spec §2). *Accept: model unit tests.*
- S1.2.2 Manifest loader for `projects/autosar-can/project.yaml` (doc URLs, ID regexes, repo+SHA, globs, model IDs, cost ceiling) w/ validation errors that name the bad field. *Accept: loads valid manifest; rejects broken ones with clear messages.*
- S1.2.3 SQLite schema + db module: requirements, code_units, threads, messages(+event JSON), report_runs, verdict_cache, embedding_cache. Design note: messages keyed so a future rollback/branching feature is not precluded (parent-message linkage possible without migration pain). *Accept: migration idempotent; CRUD tests.*

**F1.3 Document ingestion (AUTOSAR SWS PDFs)**
- S1.3.1 Fetcher: download R23-11 CAN Driver + CAN Interface PDFs to `data/docs/`, checksum, skip-if-present. *Accept: re-run is a no-op.*
- S1.3.2 PDF parser: PyMuPDF → text blocks with page + bbox. *Accept: golden page fixture parses with coordinates.*
- S1.3.3 Requirement extractor: manifest regex + layout rules → Requirement records incl. `upstream_ids` (SRS refs) and `named_symbols` (C APIs in text); writes `data/extraction_report.md` (counts + misses). *Accept: golden fixtures recall ≥90%, else LLM-fallback story is opened.*
- S1.3.4 Golden fixtures: 3–5 frozen PDF pages w/ hand-labeled expected requirements committed under `backend/tests/fixtures/golden/`. *Accept: pytest asserts exact extraction.*
- S1.3.5 Context-prose chunking: non-requirement prose (section intros, definitions) chunked as `doc_type=context` with section metadata — the self-query `doc_type` filter (S2.4.1) depends on this distinction. *Accept: fixture page yields both requirement and context chunks.*

**F1.4 Code ingestion (openAUTOSAR classic-platform)**
- S1.4.1 Repo fetch at pinned SHA into `data/repo/`, scoped to manifest globs. *Accept: pinned SHA checked out, re-run no-op.*
- S1.4.2 tree-sitter-c chunker: functions/types w/ line spans + breadcrumb headers. *Accept: golden C fixture → expected CodeUnits.*
- S1.4.3 `@req` annotation scanner → `req_annotations` on CodeUnits. *Accept: fixture with known annotations extracted.*

**F1.5 Indexing**
- S1.5.1 OpenRouter embeddings client + SQLite content-hash cache. *Accept: second run embeds 0 new chunks.*
- S1.5.2 Chroma collection per `project_id` w/ metadata; BM25 built at startup from SQLite. *Accept: startup <5s; smoke query returns known chunk.*
- S1.5.3 Ingestion CLI `python -m ingestion.run projects/autosar-can/project.yaml` end-to-end. *Accept (WP1 gate): sqlite shows N requirements; 5 spot-checked by hand against the PDFs.*

### WP2 — Retrieval & RAG Engine (~4h)

**F2.1 Exact lookup** — S2.1.1 ID normalization (`sws_can_11` → `SWS_Can_00011`) + SQLite hit + citation payload `{req_id, doc, page, bbox}`. *Accept: unit tests incl. malformed IDs.*

**F2.2 Hybrid search (bonus: hard)**
- S2.2.1 BM25 + dense search as pure functions. *Accept: each returns ranked ids on fixture corpus.*
- S2.2.2 RRF fusion. *Accept: unit test of fusion math on synthetic rankings.*

**F2.3 Query translation** — S2.3.1 multi-query expansion (3 rewrites, RAG-Fusion merge). *Accept: mocked-LLM test; vocabulary-gap demo query documented.*

**F2.4 Structured retrieval** — S2.4.1 self-query filter extraction into Pydantic (module, section, doc_type) applied as Chroma/SQLite constraints. *Accept: "scheduling service requirements"-style query filters correctly (mocked LLM).*

**F2.5 Rerank** — S2.5.1 LLM listwise rerank top-20→top-5 (cheap model, structured output). *Accept: mocked test; failure falls back to RRF order.*

**F2.6 Pipeline assembly** — S2.6.1 `search_requirements(query, filters?)` and `search_code(query|symbol)` as callable, stage-instrumented functions (each stage's IO loggable — feeds RAGAS). *Accept: integration test over fixture index.*

### WP3 — Agent & Chat API (~5h)

**F3.1 LLM client & cost metering (bonus: medium)** — S3.1.1 OpenRouter client wrapper capturing tokens+cost per call, tagged by purpose (chat/judge/rerank/embed/title). *Accept: usage events emitted per message.*

**F3.2 Agent & tools** — S3.2.1 the 3 retrieval tools (`lookup_requirement`, `search_requirements`, `search_code`) wrapping WP2 engines, with a tool registry the WP4 tools plug into later; S3.2.2 tool-calling agent loop w/ streaming callbacks. *Accept: scripted conversation calls lookup + search tools correctly (recorded LLM).*

**F3.3 Chat SSE endpoint & read API**
- S3.3.1 `POST /chat` streaming envelope `{type,data}`: `token|tool_start|tool_result|citation|usage|done|error`. *Accept: `curl -N` shows full event sequence for the two canned questions.*
- S3.3.2 Read endpoints: `GET /requirements/{req_id}` (wraps F2.1), `GET /documents/{doc}/view?highlight=req_id` (page + bbox payload), `GET /code/{path}?lines=a-b` (snapshot-resolved file slice). *Accept: endpoint tests incl. unknown ID/doc/path → clean 404.*
- S3.3.3 Error policy: OpenRouter failure/timeout mid-stream → `error` SSE event w/ user-readable message + thread left consistent; report-job failures land the job in a `failed` state with reason. *Accept: fault-injection tests (mocked client raising).*

**F3.4 Domain scoping** — S3.4.1 system prompt (domain persona, out-of-domain refusal+redirect, data-not-instructions fencing rules). *Accept: 2 out-of-domain fixtures produce scoped refusal (these two fixtures owned here, referenced by WP6 suite).*

**F3.5 Threads** — S3.5.1 threads/messages persistence storing full event streams; S3.5.2 thread CRUD API (create/list/rename/delete/get-with-events); S3.5.3 auto-title after first exchange (cheap model). *Accept: reload returns identical event stream; title generated.*

**F3.6 Setup & status API (backend for first-run)** — S3.6.1 `GET /setup/status` (index present, API key valid, counts); S3.6.2 `POST /setup/ingest` running the F1.5 ingestion pipeline as a background job + `GET /setup/ingest/events` SSE progress. *Accept: fresh `data/` dir → status reports missing → ingest job streams progress → status reports ready.*

### WP4 — Evidence & Traceability (~4h)

**F4.1 Evidence engine**
- S4.1.1 T1 annotation scan (claimed evidence w/ file:line). *Accept: fixture annotation found.*
- S4.1.2 T2 anchor retrieval (named_symbols → AST index) + hybrid semantic, ≤8 candidates. *Accept: `Can_Write` req yields the function.*
- S4.1.3 T3 tool-less LLM judge → Pydantic verdict `{status: implemented|partial|missing|unverifiable, evidence[], confidence}`. *Accept: recorded-LLM tests for all 4 statuses; malformed output retried then `unverifiable`.*

**F4.2 Report job runner**
- S4.2.1 In-process job registry + FastAPI BackgroundTask loop over scope; verdict cache keyed `(req_id, git_sha, model_id)`. *Accept: re-run of unchanged scope makes 0 LLM calls; changed `git_sha` or `model_id` re-judges (invalidation test).*
- S4.2.2 Cost estimate before launch + `MAX_REPORT_COST_USD` hard stop mid-run. *Accept: unit test aborts at ceiling.*

**F4.3 Report API & agent integration**
- S4.3.1 `POST /reports`→`{job_id, est_cost}`; SSE `{done,total,current}`; result matrix (SRS→SWS→verdict→evidence, coverage stats); export MD/CSV/JSON. *Accept: scoped run over `Can` completes; all three of implemented/partial/missing present (expected w/ version drift).*
- S4.3.2 Register `check_implementation` + `generate_traceability_report` as agent tools in the F3.2 registry (agent now has all 5). *Accept: scripted conversation triggers an evidence check and launches a report (recorded LLM).*

**F4.4 Judge evaluation against annotation ground truth (~1h)**

The C sources carry two annotation markers meaning opposite things: `@req`
claims a requirement **is** implemented, `!req` claims it explicitly is
**not**. Measured across the scoped files: **914 `@req` vs 150 `!req`**.
That is a developer-authored labelled set — positives and negatives — sitting
in the corpus for free, and it is the only ground truth the T3 judge has.
See `docs/findings/2026-08-26-corpus-and-toolchain-findings.md` §A2/A3.

- S4.4.1 Build the eval set from the ingested corpus: requirements whose
  canonical annotations resolve into the corpus, split into a **positive**
  set (annotated `@req` only) and a **negative** set (annotated `!req` and
  never `@req` anywhere). Persist it as a fixture so the eval is
  reproducible and so a re-ingest cannot silently change the denominator.
  Exclude requirements carrying both markers rather than guessing — count
  and report them. *Accept: set built with both classes non-empty; counts
  and the both-markers exclusion recorded.*
- S4.4.2 Run `check_implementation` over the set and report a confusion
  matrix over the four verdicts. Scoring rule, fixed in advance so it cannot
  be chosen to flatter the result: on the negative set, `missing` or
  `unverifiable` counts as correct and `implemented` as a false positive;
  on the positive set, `implemented` or `partial` counts as correct and
  `missing` as a false negative. Report precision and recall for
  "implemented", the `unverifiable` rate per class, and mean confidence per
  cell. Respect `MAX_REPORT_COST_USD` and cache verdicts by
  `(req_id, git_sha, model_id)` like any other run — a re-run must cost
  nothing. *Accept: matrix produced over a scoped subset; false-positive
  rate on the negative set stated explicitly.*
- S4.4.3 Write the result into the README as a table, with the honest
  caveats: the annotations describe an **older AUTOSAR release** than the
  ingested specs, so a `!req` may be stale rather than wrong; annotations
  are claims by the original developers, not verified truth; and the sample
  is one module family, not the whole standard. *Accept: table in README
  with caveats stated, not buried.*

Why this earns its hour: it converts the LLM judge from an unmeasured
component into one with a reported error rate, which is the difference
between claiming the traceability report is trustworthy and showing it.
It also directly exercises the `unverifiable` verdict the design insisted on
allowing.

### WP5 — Frontend Experience (~7h)

**F5.0 Design canvas (gate for the rest of WP5)** — S5.0.1 /design mockups: main screen (sidebar+chat+split pane), PDF vs code source states, report flow (launch/progress/matrix), first-run + empty states, checkpoint rail. *Accept: user approves canvas; WP5 implements to it.*

**F5.1 App shell & thread sidebar** — S5.1.1 three-zone responsive shell (resizable panes); S5.1.2 sidebar: thread list, auto-titles, rename-on-click, delete, new-thread. *Accept: threads survive reload.*

**F5.2 Chat pane** — S5.2.1 SSE hook + streaming markdown; S5.2.2 tool-call chips w/ live status; S5.2.3 citation chips from `citation` events (click→source pane); S5.2.4 per-message token/cost line; S5.2.5 error & loading states: `error` SSE events render as inline message + toast, skeleton loaders while panes fetch. *Accept: canned conversation renders all five; reload replays identically from stored events; killed-backend test shows toast, not a hang.*

**F5.3 Source pane (split window)** — S5.3.1 pdf.js page render + bbox highlight overlay (`/documents/{doc}/view?highlight=`); S5.3.2 code tab: Shiki-highlighted file scrolled to line span. *Accept: citation click highlights the requirement on the real PDF page; evidence click lands on the code lines.*

**F5.4 Checkpoint rail** — S5.4.1 timeline dot per user request w/ action icon (search/lookup/evidence/report); click scrolls to exchange AND restores source pane from that exchange's stored events. *Accept: manual walkthrough over a 6-turn thread.*

**F5.5 Report drawer** — S5.5.1 launch dialog (scope + cost estimate); S5.5.2 SSE progress bar; S5.5.3 matrix table w/ verdict filters, SRS tooltip chips, row-click opens both source tabs; S5.5.4 export buttons (MD/CSV/JSON). *Accept: full flow demo.*

**F5.6 First-run setup** — S5.6.1 detect missing index/API key → guided setup screen w/ ingestion progress. *Accept: fresh clone + `make dev` reaches working chat without touching a terminal error.*

**F5.7 Thread export (with F3.5 banks the "conversation history and export" easy bonus)** — S5.7.1 export thread as Markdown (citations as links) + JSON (raw events). *Accept: exported MD renders with working citation links.*

### WP6 — Security & Hardening (~2h) (bonus: medium)

**F6.1 Injection defences** — S6.1.1 fencing/framing of ALL retrieved text in prompts; S6.1.2 capability separation asserted (judge/reranker constructed tool-less). *Accept: code review + unit assertion.*
**F6.2 Adversarial test set** — S6.2.1 ~8 adversarial fixtures (poisoned code comment, poisoned doc chunk, prompt extraction, tool-forcing) asserted schema-bound/refused in pytest; suite also runs F3.4's 2 out-of-domain fixtures (owned there). *Accept: suite green.*
**F6.3 API hardening** — S6.3.1 input validation (ID regex, length caps), token-bucket rate limit on SSE, path-allowlist on `/code/{path}` (traversal rejected). *Accept: traversal + flood tests.*

### WP7 — Quality, Eval & Delivery (~3h)

**F7.1 E2E smoke** — S7.1.1 script boots both servers, runs 3 canned questions + 1 scoped report to completion. *Accept: exits 0.*
**F7.2 RAGAS eval (bonus: hard #2, cuttable)** — S7.2.1 ~25 golden Q&A authored from the SWS docs; S7.2.2 eval script: faithfulness, answer relevancy, context precision/recall; naive top-k baseline vs full pipeline. *Accept: README table with both columns.*
**F7.3 Docs & delivery** — S7.3.1 README: architecture diagram, quickstart, bonus-task mapping, honest limitations (single language, version drift, judge fallibility, SRS cite-only). Must also carry the two measured results that are evidence rather than assertion: F4.4's judge confusion matrix, and the release-drift figures from `docs/findings/2026-08-26-corpus-and-toolchain-findings.md` (§A1 the CAN Driver evidence gap, §A4 the 73% annotation join rate and what the other 27% actually is). State the code repository's licence correctly as **GPL-2.0** (§A6). *Accept: fresh-eyes read-through; reviewer can run the app from README alone.*

### Traceability: bonus tasks → stories
- Hybrid search (hard) → F2.2; RAGAS (hard) → F7.2; prompt injection (medium) → WP6; token/cost display (medium) → F3.1 + S5.2.4; source citations (easy) → S3.3.1 + S5.2.3; conversation history & export (easy) → F3.5 + F5.7.

## Verification

- After phase 1: hand-check 5 extracted requirements against PDF pages; `pytest backend/tests/test_ingestion*`.
- After phase 3: `curl -N localhost:8000/chat` shows tool_start/citation/usage events for "What does SWS_Can_00011 require?" and "How does the driver report bus-off?"
- After phase 5: scoped report over `Can` module completes; matrix contains all three verdict classes (implemented/partial/missing) — expected given version drift.
- After phase 6: injection pytest suite green.
- Full E2E: smoke script + manual demo of the three headline flows (ID lookup w/ PDF highlight; evidence check w/ code pane; full report w/ progress + export).
