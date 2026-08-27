# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**ReqTrace** — a requirements-to-code traceability chatbot for automotive embedded software (Sprint 2 course project; brief in `125.md`). It answers questions about AUTOSAR SWS requirements with source-linked citations and a split-window PDF/code preview, checks a permitted C repository for implementation evidence, and generates SRS → SWS → code traceability reports.

**Progress: WP1–WP5 are done; WP6 (security & hardening) is next.** The corpus is ingested (4 SWS documents, 1054 requirements, 1253 code units); spec §4's five retrieval stages are assembled behind `retrieval/pipeline.py`; the agent, chat SSE endpoint, read endpoints, threads and setup API are live; and all **five** tools are registered. WP4 added the three-tier evidence engine (`engines/evidence.py`), traceability reports as background jobs (`engines/report.py` + `api/reports.py`), and the judge evaluation (`evaluation/judge_eval.py`). Measured facts worth not re-deriving live in `docs/findings/2026-08-26-corpus-and-toolchain-findings.md`; C9–C12 and D3–D4 are WP3's, A7/C13/D5/D6 are WP4's.

**Frontend rules learned the hard way in WP5.** (1) This Next/React is newer than model training data (`frontend/AGENTS.md`, finding C7): React's `set-state-in-effect` and `refs` rules reject setting state in an effect body or writing a ref during render — derive or key instead. (2) A width override on a `Sheet` must carry the same `data-[side=right]:` qualifier the base class uses, or it silently loses on specificity. (3) The transcript must never depend on persistence succeeding — see `AppShell`'s `unsaved`.

**Two WP4 rules that are easy to break by accident.** (1) `engines.evidence`'s `blind` flag is a *measurement* mode for the judge evaluation only — it withholds the `@req`/`!req` evidence, which would make every shipped verdict worse; never set it in the product path. (2) The report cost ceiling is configured in two places and `engines.report.ceiling_usd` decides: the `MAX_REPORT_COST_USD` environment variable wins when set, the manifest's `limits.max_report_cost_usd` applies otherwise.

**The frontend runs live by default as of WP5.** `frontend/lib/events.ts` is the *authority* on the chat wire format (`api/chat_events.py` is the Python side; do not fork them) and `frontend/lib/reports.ts` is the same thing for reports (`api/reports.py`). `chatSourceKind()` selects the world and **`lib/threads.ts` follows it** — threads and chat must never be in different worlds. `NEXT_PUBLIC_REQTRACE_CHAT_SOURCE=canned` still replays the committed fixture with no backend, no index and no key; that path is deliberately kept working, so anything that fetches must be gated on `chatSourceKind() === "live"` or the demo fills with error panels.

## Sources of truth (read before changing anything)

1. `docs/specs/2026-08-26-reqtrace-design.md` — the approved design spec. All architectural decisions live here, including grill-session amendments (§11).
2. `.claude/plans/reqtrace-workplan.md` — the active implementation plan (work packages WP1–WP7 with features/stories and acceptance criteria). Follow story order; each story has an acceptance criterion that must pass before the story is done. The breakdown was audited for coverage — do not silently drop stories.
3. `125.md` — the course brief and evaluation criteria this project is graded against.

## Locked decisions (do not re-litigate without the user)

- **Stack:** Next.js (App Router, TS, Tailwind + shadcn/ui) frontend at `frontend/`; Python 3.12 + FastAPI + LangChain v1 backend at `backend/`; managed with `uv`. Frontend reaches backend ONLY via the Next.js `rewrites()` proxy (`/api/py/:path*` → `localhost:8000`) — no CORS config.
- **All LLM, embedding, and rerank calls go through OpenRouter** (OpenAI-compatible, `base_url=https://openrouter.ai/api/v1`). No local models, no torch. Model IDs are pinned in `projects/autosar-can/project.yaml`, never hard-coded.
- **Corpus:** AUTOSAR SWS CAN Driver + CAN Interface PDFs (R23-11, fetched from autosar.org at ingestion) traced against `openAUTOSAR/classic-platform` (C, pinned SHA). Fetched artifacts live in `data/` which is **gitignored — never commit PDFs or the cloned repo**.
- **Corpus abstraction ceiling:** downstream code depends only on the `Requirement`/`CodeUnit` Pydantic models + the `project.yaml` manifest. Format handling is a plain dict of loader functions — do NOT introduce plugin registries or adapter class hierarchies.
- **Architecture:** tool-calling agent for conversation; deterministic engines behind the 5 tools (`lookup_requirement`, `search_requirements`, `search_code`, `check_implementation`, `generate_traceability_report`). The RAG pipeline stages (multi-query → self-query filters → BM25+dense with RRF → LLM rerank) are pure functions. Requirement IDs are resolved by exact SQLite lookup, never semantic search.
- **Storage:** SQLite (registry, threads with full SSE event streams per message, verdict cache keyed `(req_id, git_sha, model_id)`, embedding cache by content hash) + Chroma (vectors, collection per project) + in-memory BM25 built at startup.
- **Chat protocol:** SSE with a `{type, data}` envelope (`token|tool_start|tool_result|citation|usage|done|error`). Citations are structured events, never parsed out of prose. Threads replay stored events on reload — UI state (tool chips, citations, cost lines, checkpoint restore) must come from those events.
- **Security posture:** retrieved documents AND code are untrusted input — always fenced as data-not-instructions in prompts. Only the chat agent has tools; the evidence judge and reranker must remain tool-less. File serving resolves against the indexed snapshot only. Cost hard stop via `MAX_REPORT_COST_USD`.
- Reports are batch jobs (FastAPI BackgroundTask + in-process registry) — no Celery/Redis. Judge verdicts allow `unverifiable`; never force a binary.

## Commands (as designed — created in WP1, keep this section updated as they land)

- `make dev` — boot backend (uvicorn :8000) + frontend (next dev) together
- `cd backend && uv run pytest` — backend tests; single test: `uv run pytest tests/test_x.py::test_name`
- `cd backend && uv run python -m ingestion.run ../projects/autosar-can/project.yaml` — the **only** ingestion entry point: fetch + parse + index + embed the corpus (idempotent — a second run downloads nothing and embeds nothing; writes `data/extraction_report.md` and `data/spot_check.md`). Useful flags: `--skip-embeddings` (no API key needed), `--force`, `--stop-after {docs,extract,code,store,embed}`, `--quiet`.
- `cd frontend && npm run dev` / `npm run build`
- `cd backend && uv run python -m evaluation.judge_eval build|run` — the judge evaluation (F4.4). `build` derives the labelled set from the corpus' `@req`/`!req` annotations and writes `tests/fixtures/judge_eval_set.json` (free); `run --limit-per-class N` judges a sample and prints the README table (**spends money** — it calls the judge model; a re-run is free because verdicts are cached).
- Requires `OPENROUTER_API_KEY` in `backend/.env` (see `.env.example`)

## Working conventions

- Conventional commits on `main`; this is the graded submission repo.
- Golden fixtures under `backend/tests/fixtures/golden/` are the contract for ingestion — extraction changes must keep them green (recall ≥90%) or update them deliberately with hand-verified labels.
- Engine tests use recorded/mocked LLM replies; tests must not hit OpenRouter.

## Rules

- Never add "Co-Authored-By" lines to commits or include Claude attribution in commit messages and PR descriptions.
- Do NOT Commit any secrets — no API keys, passwords, or tokens in the files
- After any dependency change, regenerate `dependencies.txt` with `make deps` (or `python3 scripts/gen_dependencies.py`) — never edit it by hand. `make lint` runs `make deps-check`, which fails on drift, so a stale file will be caught; adding a dependency and not regenerating went unnoticed for two work packages before that guard existed.
- Don't add any external libraries or packages without asking me first
- Never delete anything without asking for permission while providing a sentance summary of why you are deleting it
- If anything is unclear, ask me a question before making assumptions.
