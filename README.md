# ReqTrace

ReqTrace is a requirements-to-code traceability chatbot for automotive
embedded software. It answers questions about AUTOSAR SWS requirements with
source-linked citations and a split-window PDF/code preview, checks a
permitted C repository for implementation evidence, and generates
SRS → SWS → code traceability reports.

## Status — the corpus is real; the chat is not built yet

Work package 1 of 7 is done: **ingestion works end to end**, and every figure
below comes from a run on this machine rather than from a plan.

**What works today**

- Four AUTOSAR R23-11 specifications (CAN Driver, CAN Interface, CAN
  Transport Layer, CAN State Manager), fetched from autosar.org and parsed
  with page/bbox locators: **1054 requirements** (matching the manifest's
  expected count for all four documents) plus **1292 context-prose chunks**.
- The pinned `openAUTOSAR/classic-platform` snapshot, chunked with
  tree-sitter into **1253 code units** carrying **1646 requirement
  annotations** (1454 `@req` "is implemented" + 192 `!req` "is explicitly
  **not** implemented" — the two are never collapsed).
- The foundations hybrid search will stand on: 3382 embedded chunks in
  Chroma, and a 3599-record BM25 index rebuilt from SQLite at API startup in
  0.1 s. Requirement lookup is exact SQLite, never semantic.
- Evidence artifacts written on every run: `data/extraction_report.md` (miss
  list, per-document counts, named exclusions) and `data/spot_check.md`
  (hand-verifiable requirement samples).
- A frontend shell — three-zone layout, thread sidebar, chat pane, source
  pane with a working code viewer — driven by a **committed canned event
  stream**, not by the backend. It is the same reducer and the same
  components the real SSE stream will drive, which is why it exists this
  early, but nothing on that screen has been through a model.

**What does not exist yet.** Do not read the list above as more than it is:

- the retrieval pipeline (multi-query → self-query filters → BM25 + dense
  with RRF → LLM rerank)
- the tool-calling agent and its five tools (`lookup_requirement`,
  `search_requirements`, `search_code`, `check_implementation`,
  `generate_traceability_report`)
- the chat API and its SSE event stream, and thread persistence
- the LLM evidence judge, and therefore every verdict
- traceability report generation
- PDF page rendering and code serving from the indexed snapshot

**502 backend tests** pass (`make test`); ruff and the frontend's lint,
typecheck and production build are clean (`make lint`, `npm run build`).

## Quickstart

Prerequisites: [uv](https://docs.astral.sh/uv/) (Python 3.12 is pinned by
`backend/pyproject.toml`) and Node.js 20+ (verified on 26.7.0).

```bash
make install                       # uv sync (backend) + npm install (frontend)
cp backend/.env.example backend/.env   # then put your key in OPENROUTER_API_KEY
```

Every LLM, embedding and rerank call goes through OpenRouter, so
`OPENROUTER_API_KEY=sk-or-...` in `backend/.env` is required to ingest.

```bash
cd backend && uv run python -m ingestion.run ../projects/autosar-can/project.yaml
```

That one command fetches the four PDFs and the pinned repository snapshot,
extracts and chunks everything, writes SQLite + Chroma under `data/`, then
builds the BM25 index and runs a smoke query, so a run ends by proving the
index it just wrote can be searched. It is **idempotent**: a second run over
an intact `data/` downloads nothing, embeds nothing and costs nothing.
Measured here — cold 44 s and **$0.0073** of embeddings for the whole
corpus, warm 6 s and $0.00. Add `--skip-embeddings` to build SQLite and BM25
with no key and no cost — no vectors are written, so Chroma stays as it was.

`data/` is gitignored — no PDFs and no cloned repository are committed — so a
fresh clone must run ingestion before there is anything to search.

```bash
make dev     # backend on :8000, frontend on :3000
make test    # backend suite
make lint    # ruff + eslint
```

The frontend reaches the backend only through the Next.js proxy
(`/api/py/*` → `localhost:8000`), so there is no CORS configuration. The API
boots even with no database or an unreadable one, and reports that instead of
dying.

## What the numbers say, including the awkward ones

These are the product's findings, not its defects. All of them are derived in
[`docs/findings/2026-08-26-corpus-and-toolchain-findings.md`](docs/findings/2026-08-26-corpus-and-toolchain-findings.md),
and the ingestion CLI reprints the join figures on every run.

**The permitted repository contains no CAN Driver implementation.**
`openAUTOSAR/classic-platform` (**GPL-2.0**, pinned at
`09433770bebb8f27a7b480d7c96d814c68ffed3e`; fetched and analysed locally,
never redistributed) has no `Can.c` or `Can.h` anywhere. Of the CAN Driver's
**240 requirements, exactly one** — `SWS_Can_00416`, annotated in `CanIf.c` —
has any claimed code evidence. So a traceability report over the CAN Driver
will be almost entirely `missing` or `unverifiable`. That is the honest
answer for this snapshot and it is the clearest demonstration available of
what ReqTrace is for: spec/code drift, made visible and counted. The document
is deliberately **not** dropped to make the matrix look fuller.

**Tier-1 join rate: 38.0% corpus-wide, 70.3% where a specification is
ingested.** 481 of the 1267 distinct annotated ids in the code exist as
ingested requirements. The corpus-wide figure is depressed by design: CanNm,
Com and PduR contribute 583 annotated ids with no SWS document in the corpus
(they still earn their place for code search). Restricted to the four modules
whose specifications *are* ingested, 481 of 684 join.

**Release drift is measured, not a bug.** The code annotates the old AUTOSAR
4.0.3 identifiers (`CANIF023`); R23-11 spells them `SWS_CANIF_00023`.
Normalizing module + zero-padded number resolves **68.1%** of CanIf's 323
annotated ids (220), and the remaining 31.9% are requirements **deleted or
renumbered between releases** — not a normalizer failure. Per-module rates:
CanIf 68.1%, CanSM 75.3%, CanTp 67.4%.

**41 requirements have weak evidence only.** Their only claimed-implemented
annotation sits on a header prototype or a file-scope comment block rather
than on a function body. `kind` is stored per code unit precisely so the
evidence judge can tell those apart instead of reporting them as
implementation.

**`!req` is not the same as `@req`.** 192 annotations state that a
requirement is explicitly *not* implemented; 73 of them resolve to ingested
requirements. Claim polarity is carried per annotation, and tier-1 evidence
consumes only the claimed-implemented set — collapsing the two would make the
report assert implementation the original developers documented as absent.

## Where the decisions live

- [`docs/specs/2026-08-26-reqtrace-design.md`](docs/specs/2026-08-26-reqtrace-design.md) — the approved design
- [`docs/findings/2026-08-26-corpus-and-toolchain-findings.md`](docs/findings/2026-08-26-corpus-and-toolchain-findings.md) — measured corpus, toolchain and security findings
- [`docs/decisions/2026-08-26-implementation-decisions.md`](docs/decisions/2026-08-26-implementation-decisions.md) — the rulings made while implementing WP1
- [`.claude/plans/reqtrace-workplan.md`](.claude/plans/reqtrace-workplan.md) — the work breakdown, WP1–WP7
- [`CLAUDE.md`](CLAUDE.md) — the locked stack and conventions

This README covers what exists as of work package 1. The full one —
architecture diagram, bonus-task mapping, evaluation results — is story
S7.3.1 and lands with the finished system.
