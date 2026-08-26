# ReqTrace — Requirements-to-Code Traceability Chatbot: Design

Status: approved 2026-08-26 (brainstormed and reviewed section-by-section;
amended same day after a design-grill session — see §12)

## 1. Purpose

A domain-specialised chatbot for automotive embedded software that:

1. Answers questions about software requirements, retrieving the relevant
   requirement with its source link and a split-window preview of the original
   document page.
2. Checks a permitted source-code repository for implementation evidence of a
   requirement.
3. Generates a full requirements-to-code traceability report (SRS → SWS →
   verdict → evidence).

Built for the Sprint 2 project brief (`125.md`): advanced RAG with query
translation and structured retrieval, ≥3 tool calls, domain specialisation,
LangChain + OpenRouter, Next.js UI.

## 2. Corpus

Real documents and real code — no synthesized corpus:

- **Requirements:** AUTOSAR SWS *CAN Driver* and *CAN Interface* PDFs, fetched
  at ingestion time directly from autosar.org (no registration). Requirement
  IDs follow `[SWS_Can_00011]`; each requirement cites upstream `SRS_*` IDs,
  giving two-level traceability.
- **Code:** `github.com/openAUTOSAR/classic-platform` (Arctic Core lineage,
  C, GPL-2.0 — finding A6), scoped to `communication/`; the manifest's
  `include_globs` are `CanIf`, `CanTp`, `CanSM`, `CanNm`, `PduR` and `Com`.
  `Can` is deliberately absent — the repository has no CAN Driver
  implementation (finding A1), which is why a traceability report scoped to
  `Can` shows drift rather than a verdict mix. Source
  carries `/** @req ... */` annotations. The code targets an older AUTOSAR
  release than the R23-11 specs, so the traceability report surfaces *real*
  gaps and drift — which is the tool's purpose.
- Fetched artifacts live under `data/` and are **never committed**.

### Corpus abstraction

Everything downstream of ingestion depends on exactly two normalized records:

```
Requirement: id, title, text, section_path, page, bbox, source_doc,
             upstream_ids, named_symbols, version, project_id
CodeUnit:    repo_path, language, symbol, kind, line_span, text,
             req_annotations, git_sha, project_id
```

Per-corpus variation lives in `projects/<name>/project.yaml`: document URLs/
globs, requirement-ID regexes, repo URL + pinned SHA, language, include/
exclude globs, model IDs. Format handling is a plain dict of loader functions
(`{".pdf": load_pdf, ...}`) — deliberately not a plugin framework. Swapping in
another corpus later means a new manifest and at most one loader function,
plus a supported-language parser for the code side.

## 3. Architecture (Approach C)

A LangChain v1 tool-calling agent handles conversation; all heavy lifting is
done by deterministic, independently testable engines exposed as tools:

| Tool | Behaviour |
|---|---|
| `lookup_requirement(req_id)` | Normalized ID → SQLite exact hit. Never semantic. Returns requirement + citation payload. |
| `search_requirements(query, filters?)` | The advanced-RAG pipeline as one tool (see §4). |
| `search_code(query \| symbol)` | AST-chunk retrieval over the pinned repo snapshot only. |
| `check_implementation(req_id)` | Three-tier evidence engine (see §5). |
| `generate_traceability_report(scope)` | Launches a deterministic batch job with SSE progress (see §5). |

LLM, embeddings, and reranking all go through OpenRouter (single provider,
OpenAI-compatible; embeddings via `/api/v1/embeddings`, model ID pinned in
the manifest, embedding cache keyed by content hash). No local model
dependencies (no torch).

**Storage:** SQLite (registry, report runs, embedding cache) + Chroma
(vectors, one collection per project) + `rank_bm25` built in-memory at API
startup.

## 4. Advanced RAG pipeline (`search_requirements`)

Chunking: **one requirement = one chunk** with full metadata; surrounding
prose chunked separately as `context`. Code: one AST unit = one chunk with a
breadcrumb header (`// Can.c > Can_Write`).

Stages (each a pure function, evaluated by RAGAS):

1. **Multi-query expansion** (RAG-Fusion): 3 LLM rewrites bridging user
   vocabulary ↔ spec vocabulary; each retrieves independently.
2. **Self-query structured filters:** LLM extracts a typed Pydantic filter
   (module, section, doc_type) applied as metadata constraints.
3. **Hybrid search:** BM25 + dense in parallel, fused with Reciprocal Rank
   Fusion. BM25 catches exact tokens (`CRC`, `Can_Write`, `ST[11]`); dense
   catches paraphrase.
4. **Rerank:** LLM listwise rerank (cheap fast model) of top-20 fused → top-5.

## 5. Evidence engine & traceability report

`check_implementation(req_id)` — cheapest evidence first:

- **T1 annotation scan:** `@req` references in code = *claimed* evidence
  (recorded with file:line, still verified by T3).
- **T2 anchor retrieval:** requirement's `named_symbols` pull exact
  `CodeUnit`s; hybrid search adds semantic candidates; ≤8 total.
- **T3 LLM judge (tool-less):** structured verdict
  `{status: implemented|partial|missing|unverifiable, evidence:[{file, lines,
  rationale}], confidence}` — Pydantic-validated; `unverifiable` is an allowed
  honest answer.

`generate_traceability_report(scope)`: FastAPI BackgroundTask (in-process job
registry — no Celery) iterating all requirements in scope through the evidence
engine. Verdicts cached by `(req_id, git_sha, model_id)`. Cost estimate shown
before launch; `{done, total, current}` streamed over SSE; output matrix
persisted with coverage stats; export to Markdown/CSV/JSON.

## 6. API

- `POST /chat` → SSE envelope `{type, data}`: `token`, `tool_start`,
  `tool_result`, `citation`, `usage`, `done`, `error`.
- `GET /requirements/{req_id}`
- `GET /documents/{doc}/view?highlight=req_id` → page + bbox for pdf.js
- `GET /code/{path}?lines=a-b` (resolved against indexed snapshot only)
- `POST /reports` → `{job_id, est_cost}`; `GET /reports/{id}/events` (SSE);
  `GET /reports/{id}`; `GET /reports/{id}/export?fmt=md|csv|json`
- Threads: `POST /threads`; `GET /threads`; `GET /threads/{id}` (with stored
  events); `PATCH /threads/{id}` (rename); `DELETE /threads/{id}`;
  `GET /threads/{id}/export?fmt=md|json`
- Setup: `GET /setup/status`; `POST /setup/ingest`;
  `GET /setup/ingest/events` (SSE ingestion progress — backend for the
  first-run experience)

Frontend reaches the backend through a Next.js `rewrites()` proxy
(`/api/py/:path*` → `localhost:8000`) — no CORS configuration.

## 7. Frontend

Single page, three zones:

- **Chat pane:** streamed tokens, live tool-call chips, structured citation
  chips (from SSE `citation` events, never parsed from prose), per-message
  token/cost line.
- **Source pane:** Tab A renders the real SWS PDF page via pdf.js with a
  highlight overlay from the stored bbox; Tab B shows the code file (Shiki)
  scrolled to the evidence line span.
- **Report drawer:** scope + cost estimate → SSE progress bar → filterable
  matrix; row click opens both tabs; export buttons.

Plain React state + one SSE hook.

## 8. Security & prompt injection

Threat model: **the repo and documents are untrusted input.**

- Retrieved text enters prompts fenced and framed as data-not-instructions.
- Capability separation: only the chat agent has tools; judge and reranker
  are tool-less structured calls — injected text can corrupt one verdict,
  never trigger an action.
- Manifest pins repo URL + SHA; file serving rejects paths outside the
  indexed snapshot.
- Input validation (ID regex, length caps) and an in-memory token-bucket rate
  limit on SSE endpoints.
- `tests/injection_set/`: ~10 adversarial fixtures asserted schema-bound in
  pytest.

## 9. Testing & evaluation

- Golden fixtures: frozen PDF pages with hand-labeled expected requirements
  (corpus-swap insurance); known C files → expected CodeUnits; RRF math; ID
  regex edges.
- Engine tests with recorded LLM replies; cache invalidation by SHA/model.
- E2E smoke script: boot both servers, three canned questions, one scoped
  report to completion.
- **RAGAS** (sequenced last, cuttable): ~25 golden Q&A; faithfulness, answer
  relevancy, context precision/recall; README table comparing naive top-k
  baseline vs. the full pipeline.

## 10. Bonus tasks designed in

Hybrid search (hard), RAGAS evaluation (hard), prompt-injection protection
(medium), token/cost display (medium), source citations (easy).

## 11. Threads, checkpoints & UX decisions (grill-session amendments)

The quality bar is an app with very good UX, not a homework demo:

- **Persisted threads:** conversations stored in SQLite. Each assistant
  message persists its **full SSE event stream** (tokens, tool events,
  citations, usage), so reloading a thread replays tool chips, citation
  chips, and cost lines exactly — and thread export keeps citations intact.
- **Thread sidebar:** LLM auto-titles (cheap model, one call per thread),
  rename-on-click; thread export as Markdown (readable, citations as links)
  and JSON (raw events).
- **Checkpoints:** a timeline rail beside the thread — one dot per user
  request, icon indicating the action (search / lookup / evidence check /
  report). Clicking scrolls to that exchange **and restores the source pane**
  to what it showed at that moment (pure replay of stored events). No
  conversation rollback/branching in v1; the schema must not preclude it.
- **First-run experience:** a setup screen that detects a missing index or
  API key and walks through ingestion with progress — never a stack trace.
- **Styling:** Tailwind + shadcn/ui (drawer, tabs, resizable split panes,
  data table, toasts, skeleton loaders).
- **Models (pinned in project.yaml):** chat `google/gemini-2.5-flash`
  (amended 2026-08-26 during WP3 — the grill session chose
  `anthropic/claude-sonnet-4.5`, but the account's OpenRouter data policy
  blocks every Anthropic route; finding D4 records what is reachable and the
  measurements behind the replacement);
  judge/rerank `openai/gpt-4o-mini`-class; embeddings
  `openai/text-embedding-3-small`. Cost target ≤$0.50 per scoped report run;
  hard stop `MAX_REPORT_COST_USD` (default $2).
- **Out-of-domain queries:** polite scoped refusal with redirect, enforced in
  the system prompt, covered by behaviour fixtures.
- **Design-first:** a visual design canvas (main screen, split-window states,
  report flow, first-run/empty states) is produced and approved before
  frontend implementation; the frontend is built to the approved mockup.
- SRS IDs render as tooltipped chips ("upstream requirement, not ingested").
- Local-only deployment; the repo is the submission monorepo (`frontend/` +
  `backend/` at root, `main` branch, conventional commits, `data/` ignored).

## 12. Known limitations (state in README)

Single code language supported at a time; spec/code version drift (feature,
but must be framed); LLM judge fallibility (`unverifiable` verdict + human-
readable rationale as mitigations); scoped ingestion (2 SWS documents, not
the full AUTOSAR release).
