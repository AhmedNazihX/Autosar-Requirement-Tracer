# ReqTrace — corpus, toolchain & security findings

Measured 2026-08-26 during WP1. Everything here comes from probing the real
AUTOSAR documents, the real `openAUTOSAR/classic-platform` repository and the
real toolchain — not from reasoning about the plan. Figures are reproducible.

This is the companion to two other documents: `docs/specs/2026-08-26-reqtrace-design.md`
is the approved design, and `.claude/plans/reqtrace-workplan.md` is the work
breakdown. This file is what measurement changed about both.

Each entry states what is true, why it matters, and what to do. Sections are
ordered by how much they change what gets built: **A** changes the product's
story and belongs in the README; **B** is settled extraction behaviour
recorded so it is never re-derived; **C** is toolchain and security; **D** is
cost; **E** is the deferred-minor triage list; **F** is process.

## A. Findings that change the product's story

### A1. The permitted repository contains no CAN Driver implementation
**Measured.** No `Can.c` or `Can.h` exists anywhere in
`openAUTOSAR/classic-platform` at the pinned SHA — `master` is the only
branch, and `arch/` holds only `generic/linux` and `transceivers/tja1145`.
Of the CAN Driver's 240 requirements, exactly **one** (`SWS_Can_00416`, cited
from `CanIf.c`) has any tier-1 code evidence.

**Why it matters.** One of the two headline documents will return ~239/240
`missing` or `unverifiable`. Presented naively that reads as a broken tool;
framed correctly it is the single best demonstration of what the product is
*for* — spec/code drift made visible and quantified.

**To do.** The README must state this plainly and early (story S7.3.1's
"honest limitations" already reserves the place). The report UI already
solves the presentation side per ruling R20. Do **not** quietly drop the CAN
Driver document to make the matrix look better.

### A2. `!req` means the opposite of `@req`, and the plan never noticed
**Measured.** The C sources carry two markers: `@req` claims a requirement
*is* implemented, `!req` claims it explicitly is **not**. Counts across the
five scoped `.c` files: **914 `@req` vs 150 `!req`** (14% of annotations).
Neither the plan nor the spec mentions `!req`.

**Why it matters.** Collapsing them makes the traceability report assert
implementation for requirements the original developers documented as
unimplemented — a wrong answer in the product's core output, and one a
grader could catch by opening the source.

**To do.** Already handled in the model and manifest (ruling R12): claim
polarity is carried per annotation and tier-1 evidence consumes only the
claimed-implemented set. **Still to exploit:** see D2 below.

### A3. `!req` is free negative ground truth for the LLM judge — unexploited
The 150 `!req` annotations are developer-authored statements that a
requirement is *not* implemented. That is a labelled negative set, in the
real corpus, for free.

**Why it matters.** WP4's judge (`check_implementation`) currently has no
ground truth at all, and story S4.1.3 tests it only against recorded LLM
replies. The `@req`/`!req` split gives both classes.

**To do.** In WP4, evaluate the judge against this set: for requirements
annotated `!req` with no other evidence, the judge should return `missing`
or `unverifiable`, not `implemented`. Report precision/recall in the README.
This is a genuine evaluation result rather than an assertion, and it costs
almost nothing to produce. Strong candidate for the RAGAS section (F7.2) or
a section of its own.

### A4. The 4.0.3 → R23-11 identifier drift *is* the thesis, and it is measurable
**Measured.** Code annotations use the old AUTOSAR 4.0.3 form (`CANIF023`);
the R23-11 specifications use `SWS_CANIF_00023`. Normalizing module +
zero-padded number resolves **68.1%** of CanIf's 323 annotated IDs (220).
The **~32% that do not resolve are requirements deleted or renumbered
between releases** — genuine drift, not a bug in the normalizer.

**Why it matters.** That unresolved third is the most concrete evidence the
product works. It is also the number most likely to be mistaken for a defect
by a reader (or a grader) who does not know it was measured deliberately.

**On the figure itself, because this document previously contradicted
itself.** An earlier revision said 73%, which came from the very first
measurement over five `.c` files only (179 of 245). §A5's table then said
68% (186 of 271) from the full-corpus scan including headers. The running
pipeline now reports **68.1% (220 of 323)** — a third denominator again,
because the `prototype` and `file` unit kinds changed what counts as an
annotation site. All three are "correct" over their own denominator, which is
exactly how a figure quietly becomes wrong when quoted without one.

**The authoritative number is whatever the pipeline reports**, because it is
the only one derived from the shipped code rather than from an exploratory
script. Quote it with its denominator, and prefer regenerating over copying:
`uv run python -m ingestion.run ../projects/autosar-can/project.yaml
--stop-after store` prints the join rates.

This correction was caught by an implementer that was handed 73% for the
README and refused it, on the grounds that putting an unverified figure into
the file being fixed *for honesty* would reintroduce the defect. It was
right.

**To do.** Surface it as a first-class output: the extraction/ingestion
report should list annotated IDs that resolve to no requirement in the
corpus, grouped by module, labelled as release drift rather than as errors.
Cite the figure in the README.

### A5. Corpus-wide tier-1 join rate is 38%, 70% for modules whose specs are ingested
**Measured by the shipped pipeline**, over the whole indexed corpus:

| Scope | annotated IDs | resolve to a requirement |
|---|---|---|
| corpus-wide | 1267 | **481 (38.0%)** |
| modules whose specs are ingested | 684 | **481 (70.3%)** |
| `CanIf` | 323 | 220 (68.1%) |
| `CanSM` | — | 75.3% |
| `CanTp` | — | 67.4% |
| `CanNm`, `Com` | 272 | **0** — specs not ingested |
| `Can` | 1 | 1 |

Regenerate rather than copy these:
`uv run python -m ingestion.run ../projects/autosar-can/project.yaml --stop-after store`.

**An earlier revision of this section said 50% (435 of 858) and is
superseded.** That figure came from an exploratory scan of five `.c` files
before headers were indexed and before the `prototype` and `file` unit kinds
existed, so its denominator counted fewer annotation sites. Both numbers are
"right" over their own denominator, which is precisely how a metric goes
stale without one — see §A4 for the same trap hit twice. **The pipeline's
figures are authoritative**, because they are the only ones derived from
shipped code; the README quotes these and nothing else.

**Why it matters.** `CanNm` and `Com` contribute 272 annotated IDs that
resolve to nothing, because their SWS documents are not ingested. They still
earn their place for `search_code` and tier-2 semantic evidence, but they
add no traceability. The gap between 38% corpus-wide and 70% for
spec-ingested modules is entirely those two modules plus header-only IDs —
so quoting 38% alone understates the pipeline, and quoting 70% alone
overstates its coverage. Both belong together.

**To do.** Optional and cheap: adding the CanNm and Com SWS documents is a
**manifest edit only** — no code change — and would lift the join rate
further. Both fetch fine from the same URL pattern. The owner chose four
documents deliberately; this is the lever if the matrix should look fuller.

### A6. The repository is GPL-2.0, not "Apache-style" as the plan says
**Measured** from the GitHub API. We fetch and analyse locally and never
redistribute, so this is a labelling correction, not a licensing problem.

**To do.** Correct it in the README. Do not repeat the plan's wording.

## B. Extraction findings (all already encoded — recorded so they are not re-derived)

### B1. The requirement block format, and why each part is load-bearing
Format: `[<ID>] <optional title> ⌈<text>⌋(<optional upstream refs>)`, where
`⌈` is U+2308 and `⌋` is U+230B. Measured recall when dropping each element:

| Omitting | Recall falls to |
|---|---|
| the cleaning pass | 73% / 76% |
| the `title` group | 73% / 76% |
| the `_CONSTR` alternative | ~97% |
| nothing | **99.6% / 99.7%** |

Per-document results: CAN Driver 240/241 ceilings, CAN Interface 398/399,
CanTp 175/176, CanSM 241/241 — **1054 requirements**.

### B2. Upstream references are not only `SRS_`
`RS_Ids_00810` (Intrusion Detection System) genuinely appears. The pattern
must be `(?:SRS|RS)_[A-Za-z]+_\d+`. Getting this wrong silently drops
upstream links, which is half the two-level traceability story.

### B3. Requirement ID casing differs per document and must not be normalized
`SWS_Can_00011` and `SWS_CanTp_00002` and `SWS_CanSM_00008` are mixed-case;
`SWS_CANIF_00001` is upper-case. Citations must render exactly as the spec
writes them. The manifest carries a per-document `req_id_pattern` for this,
and `canonical_id()` exists for case-insensitive *matching* only.

**Watch for.** This was nearly a silent failure: had CanTp/CanSM used
CanIf's upper-case form, the annotation module map would have been wrong and
the joins the owner paid for would have quietly produced nothing. Verified
before committing the manifest.

### B4. One extra `⌈` per document — needs a named exclusion, not a `-1`
Three of four documents contain exactly one more ceiling than their expected
requirement count. **To do (D4):** find what each actually is and name and
justify each exclusion in `extraction_report.md`. A magic constant or a
special case keyed on document name is a defect. D3's fixture notes point at
the shapes responsible: `can_driver_p034` flags `[SWS_Can_91011]`/`91012`
as *references, not requirements*; `can_driver_p035` flags prose continuing
**after** the closing floor.

### B5. 60–62% of requirements name a C symbol — tier-2 anchoring is viable
Pattern in the manifest; ~45 distinct symbols in CAN Driver, ~87 in CAN
Interface (`Can_Write`, `CanIf_RxIndication`, `Det_ReportError`, …). This is
what tier-2 evidence retrieval keys on, so it is load-bearing for the
requirements that have no annotation.

### B6. De-hyphenation is a corpus-quality issue, not just a recall one
Justified PDF text breaks words *and identifiers* across lines —
`RX indica-\ntion`, `Can_-\nSetControllerMode`. Without rejoining, the index
contains broken tokens that degrade both BM25 matching and embeddings.

## C. Toolchain and security findings

### C1. `www.autosar.org` serves an incomplete TLS chain — and curl hid it
**Measured.** The server sends only its leaf certificate. `openssl s_client`
returns verify code 21; plain `httpx` fails with
`CERTIFICATE_VERIFY_FAILED`. My own pre-flight check used **curl, which
chases AIA**, so it reported 200 and I wrote that false assurance into two
briefs. Fixed by pinning the missing intermediate (commit `3dcb980`).

**Lesson worth keeping.** Verifying a network dependency with a different
HTTP client than the one the code will use is not verification. Check with
the actual stack.

### C2. The first TLS fix was a trust-anchor injection vector
The AIA-chasing implementation passed a plain-HTTP-fetched certificate to
`load_verify_locations(cadata=...)`, installing it as a **root** rather than
an untrusted intermediate, after re-probing with `CERT_NONE`. An on-path
attacker could have had their own CA trusted and completed a MITM with
`check_hostname` still on.

**Lesson worth keeping.** This was found by review, not by the implementer,
who had flagged a different part (its DER parsing) as the risk. Security
fixes need an independent reader; self-review would have shipped it.

### C3. `chromadb` pulls `onnxruntime` — never let a collection use a default embedding function
chromadb 1.5.9 → onnxruntime 1.29.0 (no torch, no sentence-transformers).
That runtime backs Chroma's *default* embedding function, which would embed
with a local MiniLM into a **different vector space** than
`text-embedding-3-small`, silently corrupting retrieval.
**To do (D6):** pass `embeddings=` explicitly on every add/upsert, construct
collections so an omission fails loudly, and assert in a test that no
default embedding function is attached.

### C4. `BM25Okapi` returns all-zero scores on tiny corpora
Okapi IDF is `log((N-n+0.5)/(n+0.5))`, exactly **0** at `N=2, n=1`. Measured
on one query: N=2 → all scores 0; N=4 → top 2.905; N=8 → top 4.41.
**To do (D6):** BM25 fixtures need **≥8 documents** and must assert ranking
order, not absolute scores. A zero here is not a tokenizer bug — do not
"fix" a healthy identifier tokenizer chasing it.

### C5. C symbol names are not unique within a file, and naming them is subtle
`typedef struct { uint8 a; } Can_PduType;` emits two AST nodes over the same
lines; `struct CanIf_Global {…};` plus its typedef emitted **three** nodes
named `CanIf_Global`. Fixed in the schema (`code_units` keyed on
`(project_id, repo_path, kind, symbol, line_span_start, line_span_end)`,
commit `86812f6`) — without it, upsert silently discarded real annotations.
Separately: a naive "first identifier descendant" name walk returns
**`uint8`** for that typedef — the field's type, not the typedef's name.
**To do (D5):** take the declarator name specifically; test typedef'd
anonymous structs and enums. A mis-named unit is worse than a missing one,
because it poisons symbol-anchored retrieval with a plausible wrong answer.

### C6. Shallow fetch-by-SHA works against this repository
`git init` + `git fetch --depth 1 origin <sha>` + `git checkout FETCH_HEAD`
yields the exact pinned commit, 23 MB on disk. No full or branch clone
needed.

### C7. Next.js 16.3.3 / React 19.2.8 are newer than model training data
`next dev` writes `frontend/AGENTS.md` instructing that the guides in
`node_modules/next/dist/docs/` be read before writing Next.js code.
**To do:** carry this into every frontend dispatch. Code written from memory
here looks plausible and is wrong.

### C8. Shiki's `FontStyle` is a bitmask
Italic 1, Bold 2, Underline 4. Comparing `=== 3` for italic-and-bold drops
italic+underline (5) and italic+bold+underline (7). Fixed in `1adb47c`,
**but that fix has not been through review** — the F5.1 reviewer should
check it.

## D. Cost and metering findings

### D1. OpenRouter returns actual cost — do not build a price table
A live embeddings call returned
`{'prompt_tokens': 14, 'total_tokens': 14, 'cost': 2.8e-07, …}`.
**To do (F3.1 / S5.2.4):** report OpenRouter's own `cost` figure rather than
estimating from hard-coded per-model prices. More accurate, and nothing to
maintain as prices change.

### D2. Ingestion is effectively free; the cost ceiling is about judging
Extrapolating that rate, embedding the whole 1054-requirement corpus costs
well under one cent. `MAX_REPORT_COST_USD` therefore governs **judge calls
during report generation**, not ingestion. Size the pre-launch cost estimate
accordingly — estimating ingestion cost would be noise.

## E. Deferred minors (from reviews — triage before merge)

- **D1:** `make lint` runs only backend ruff, silently skipping the
  frontend's working `npm run lint`.
- **D1:** `frontend/app/page.tsx` hardcodes `bg-zinc-50`/`dark:bg-black`
  instead of the semantic tokens the same commit wired up. WP5 replaces this
  page anyway — just do not inherit the pattern.
- **D1:** `starlette.testclient` emits an httpx/httpx2 deprecation warning;
  fixing it needs an unapproved library.
- **D2:** the 40-hex `git_sha` validator is duplicated verbatim in
  `models.py` and `manifest.py` — extract a shared helper.
- **D2:** `manifest.resolve()` has no test exercising real path joining.
- **D2:** `bbox`/`char_span` stored as JSON text, so not SQL-queryable;
  `verdict_cache`/`embedding_cache` payloads are unvalidated JSON blobs.
  **WP4/WP5 must validate what they write.**
- **D3:** the "documents the normalization" test only asserts
  `"never" in readme.lower()` — it would pass with the section deleted.
- **D3:** no synthetic-PDF case for a page contributing zero blocks, so
  `page_for_char`'s duplicate-`char_start` path is covered only by a test
  that skips on a fresh clone.
- **D3:** `README.md` says `../projects/…` while `regenerate.py` prints
  `projects/…`; one is wrong depending on cwd.
- **D3:** `test_golden_fixtures.py` does manifest and JSON I/O at import
  time, so a missing manifest is a collection error, not a test failure.
- **D3:** 6 non-pristine warnings (5 PyMuPDF SWIG, 1 Starlette).

## F. Process notes worth keeping

- **Independent verification caught things reports did not.** I re-ran every
  suite myself and separately proved the D2↔D4 contract by building 240 real
  `Requirement` objects and round-tripping them through SQLite. Reports are
  claims; the commands are evidence.
- **The golden fixtures were verified as genuinely hand-made** — I compared
  one byte-for-byte against an extraction derived independently during
  planning, and the reviewer separately spotted preserved source typos and
  prose notes no extractor could produce. Fixtures generated from the code
  they validate are worthless, so this check is worth repeating whenever
  fixtures change.
- **One false alarm of my own**, recorded as a caution: I briefly believed
  requirement lookup was broken, having passed `project_id` and `req_id` in
  the wrong order to a signature I had not read. Verify the call before
  reporting the bug.
