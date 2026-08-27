# ReqTrace

ReqTrace is a requirements-to-code traceability chatbot for automotive
embedded software. It answers questions about AUTOSAR SWS requirements with
source-linked citations and a split-window PDF/code preview, checks a
permitted C repository for implementation evidence, and generates
SRS → SWS → code traceability reports.

## Status — the backend is complete; the frontend is still on fixtures

Work packages 1–4 of 7 are done: **ingestion, retrieval, the agent and the
evidence/traceability engine all work end to end**, and every figure below
comes from a run on this machine rather than from a plan.

**What works today**

- Four AUTOSAR R23-11 specifications (CAN Driver, CAN Interface, CAN
  Transport Layer, CAN State Manager), fetched from autosar.org and parsed
  with page/bbox locators: **1054 requirements** (matching the manifest's
  expected count for all four documents) plus **1292 context-prose chunks**.
- The pinned `openAUTOSAR/classic-platform` snapshot, chunked with
  tree-sitter into **1253 code units** carrying **1646 requirement
  annotations** (1454 `@req` "is implemented" + 192 `!req` "is explicitly
  **not** implemented" — the two are never collapsed).
- The advanced RAG pipeline: multi-query expansion → self-query filters →
  BM25 + dense retrieval fused with RRF → LLM listwise rerank. Every stage is
  instrumented and every stage degrades on its own terms, so a search
  survives all three model calls failing. Requirement lookup is exact SQLite,
  never semantic.
- A tool-calling agent with all **five tools**, streaming over SSE with
  structured citation events, per-turn cost, and threads that replay their
  stored event stream exactly.
- The three-tier evidence engine — annotation scan, symbol/semantic anchors,
  then a tool-less LLM judge returning
  `implemented | partial | missing | unverifiable` — and traceability reports
  as background jobs with a pre-launch cost estimate, a `MAX_REPORT_COST_USD`
  hard stop, and Markdown/CSV/JSON export.
- Evidence artifacts written on every run: `data/extraction_report.md` (miss
  list, per-document counts, named exclusions) and `data/spot_check.md`
  (hand-verifiable requirement samples).

**What does not exist yet**

- The frontend is still driven by a **committed canned event stream**, not by
  the backend. The layout, thread sidebar, chat pane and code viewer are real
  and use the same reducer the live SSE stream will, but nothing on that
  screen has yet been through a model. Wiring it up is work package 5.
- Prompt-injection hardening as a tested suite (WP6) and the RAGAS
  retrieval evaluation (WP7).

**1033 backend tests** pass (`make test`); ruff and the frontend's lint,
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

## Does the LLM judge actually work? Measured, both ways

The traceability report's verdicts come from an LLM judge, so "the report is
trustworthy" is a claim that needs a number behind it. The corpus supplies
one for free: the C sources carry **`@req`** ("this implements it") and
**`!req`** ("this is explicitly *not* implemented"), which is a
developer-authored labelled set with both classes in it.

Built from the ingested corpus: **408 positives** (`@req` only), **73
negatives** (`!req`, never `@req`), **0** carrying both markers, and **786**
annotated ids that resolve to no R23-11 requirement at all. The set is
committed as `backend/tests/fixtures/judge_eval_set.json` so a re-ingest
cannot move the denominator. The scoring rule was fixed in code *before* the
run: on positives `implemented`/`partial` is correct and `missing` is a false
negative; on negatives `missing`/`unverifiable` is correct and `implemented`
is a false positive.

**The judge was run twice, and the difference is the interesting part.** A
code unit's span starts at its attached comment block, so in normal operation
the `@req`/`!req` marker is *inside* the source the judge reads — it can see
the answer key. The **blind** run withholds the claim label and redacts the
marker from the source; the **sighted** run is the product exactly as it
ships.

| mode | class | n | implemented | partial | missing | unverifiable | correct | false positives |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| sighted | positive (`@req`) | 73 | 34 | 26 | 10 | 3 | 82% | — |
| sighted | negative (`!req`) | 73 | 0 | 0 | 73 | 0 | 100% | **0** |
| blind | positive (`@req`) | 73 | 29 | 30 | 8 | 6 | 81% | — |
| blind | negative (`!req`) | 73 | 6 | 18 | 35 | 14 | 67% | **6** |

- **sighted** — precision of `implemented` **1.00**, recall 0.47,
  false-positive rate on the negative set **0.0%**, `unverifiable` 4% of
  positives / 0% of negatives. $0.0666.
- **blind** — precision of `implemented` **0.83**, recall 0.40,
  false-positive rate on the negative set **8.2%**, `unverifiable` 8% of
  positives / 19% of negatives. $0.0626.

Judge `openai/gpt-4o-mini`, snapshot `09433770bebb`. Reproduce with
`cd backend && uv run python -m evaluation.judge_eval run --limit-per-class 73`;
the full result is in
[`docs/evaluations/judge-eval.json`](docs/evaluations/judge-eval.json).

**Read this with the caveats, which are not small.**

1. **The sighted 100% is obedience, not accuracy.** The sighted judge agreed
   with every single `!req` because it could read the `!req`. That column
   measures whether the judge respects and passes through what the developers
   documented — a real property, and the one the shipped product has — but it
   is not evidence that the judge can analyse code. The blind row is the one
   to quote as an error rate.
2. **The annotations describe an older AUTOSAR release.** They were written
   against 4.0.3; the specifications here are R23-11. A `!req` may be stale
   rather than wrong, so some of the blind run's six "false positives" may be
   the judge being right about code the annotation no longer describes. The
   figure is an upper bound on error, not a count of mistakes.
3. **Annotations are claims, not verified truth.** Nobody re-checked them
   against the code; they are what the original developers believed.
4. **The sample is one module family** (CanIf, CanSM, CanTp), not the AUTOSAR
   standard, and 73 negatives is every negative that exists — it cannot be
   made larger without a different corpus.
5. **Recall is low in both modes (0.47 / 0.40)** because `partial` is counted
   separately: 26–30 of the 73 positives came back `partial` rather than
   `implemented`. Under the scoring rule those are *correct*, which is why the
   correct column reads 82%/81% while recall of the strict `implemented` label
   reads ~0.4.

The `unverifiable` verdict the design insisted on allowing is doing real work
here: 19% of blind negatives, where the judge could not tell from the code
alone and said so instead of guessing.

## A real report, end to end

A scoped run over **CanIf** — 398 requirements, judged against the pinned
snapshot:

| verdict | requirements |
| --- | ---: |
| implemented | 98 |
| partial | 59 |
| missing | 177 |
| unverifiable | 64 |

157 rows carry at least one piece of cited code evidence. The run took 9.6
minutes and cost **$0.1371** against a $2.00 ceiling, quoted beforehand at
$0.0950 from OpenRouter's own catalogue prices — a 1.44x under-quote that has
since been corrected in the estimator's token-density constant.

**Re-running the identical scope cost $0.00** and made no model calls at all:
all 398 verdicts came back from the cache, keyed
`(req_id, git_sha, judge_model_id)`. Change the pinned snapshot or the judge
model and every one of them is re-judged. The committed exports below are that
replay, so their header reads `$0.0000` — the $0.1371 above is what producing
the verdicts cost the first time.

Exports: [`docs/evaluations/canif-report.md`](docs/evaluations/canif-report.md),
[`.csv`](docs/evaluations/canif-report.csv),
[`.json`](docs/evaluations/canif-report.json).

## Where the decisions live

- [`docs/specs/2026-08-26-reqtrace-design.md`](docs/specs/2026-08-26-reqtrace-design.md) — the approved design
- [`docs/architecture/retrieval.md`](docs/architecture/retrieval.md) — what runs when a question arrives: exact lookup vs. hybrid search, and how BM25 and dense retrieval are fused
- [`docs/findings/2026-08-26-corpus-and-toolchain-findings.md`](docs/findings/2026-08-26-corpus-and-toolchain-findings.md) — measured corpus, toolchain and security findings
- [`docs/decisions/2026-08-26-implementation-decisions.md`](docs/decisions/2026-08-26-implementation-decisions.md) — the rulings made while implementing WP1
- [`.claude/plans/reqtrace-workplan.md`](.claude/plans/reqtrace-workplan.md) — the work breakdown, WP1–WP7
- [`CLAUDE.md`](CLAUDE.md) — the locked stack and conventions

This README covers what exists as of work package 4. The full one —
architecture diagram, bonus-task mapping, the RAGAS baseline comparison — is
story S7.3.1 and lands with the finished system.
