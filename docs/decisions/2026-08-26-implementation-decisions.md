# ReqTrace — decisions made during implementation

Every decision taken on the repo owner's behalf while executing
`.claude/plans/reqtrace-workplan.md`, in the order made, with what it costs
if wrong. Recorded here rather than only in the agent ledger because the
ledger lives under a gitignored scratch directory: these are decisions about
the product, and they should outlive any working directory.

Each entry states what was decided, why, and the cost of being wrong — so a
reader can reverse one without reconstructing the reasoning first.

**The four most worth a second opinion**, because they trade something real:

- **R11** — golden fixtures are frozen parser output, not PDF pages. Avoids
  committing copyrighted AUTOSAR pages, at the cost of pinning the parser's
  output format rather than its input.
- **R24** — the corpus fetcher pins a TLS intermediate and drops the OS
  trust store. More reproducible, but ingestion will fail behind a corporate
  MITM proxy that previously worked.
- **R28** — an accepted bounded loss of up to 200 characters of context
  prose per occurrence (zero occurrences in the current corpus), because the
  alternative rendered the rule inert.
- ~~**R27** — the checkpoint rail deviates from the approved design
  canvas.~~ **Resolved:** the owner reviewed the canvas on 2026-08-26 after
  being told about the deviation and its reasoning, and accepted it. The
  literal design left most dots unreachable, which defeated the behaviour
  spec §11 requires of the rail. Not an open question — do not re-raise.

Related: `docs/findings/2026-08-26-corpus-and-toolchain-findings.md` records
what measurement revealed about the corpus and toolchain; this file records
what was decided in response.

---

Ruling R1: S1.2.2 owns authoring `projects/autosar-can/project.yaml` itself,
not just the loader — no other story creates it and six stories read it.
Cost if wrong: none; the file is required either way.

Ruling R2: execute S1.3.4 (golden fixtures) **before** S1.3.3 (extractor).
S1.3.3's acceptance criterion is measured against S1.3.4's fixtures, so the
plan's order asks the extractor to pass a test that does not exist yet; this
is also the TDD order the project's own conventions favour. Cost if wrong:
none — the same two artifacts exist at the end either way.

Ruling R3: pin the two document URLs verbatim as verified above rather than
deriving them from a pattern. Cost if wrong: a 404 at ingestion, caught by
S1.3.1's own acceptance.

Ruling R4: pin `git_sha: 09433770bebb8f27a7b480d7c96d814c68ffed3e` — the
only branch's HEAD. Cost if wrong: none material; re-pinning is a manifest
edit and the verdict cache keys on the SHA, so it self-invalidates.

Ruling R5: **drop `Can` from the code include-globs and scope the code side
to `communication/{CanIf,CanTp,CanSM,CanNm,PduR,Com}`.** The plan's
`communication/(Can, CanIf, PduR, Com)` names a module with no
implementation in this repo. The locked *corpus* decision is unchanged —
both SWS PDFs are still ingested — but CAN Driver requirements will resolve
predominantly to `missing`/`unverifiable`, which is the drift the spec says
the tool exists to surface. The README must state this plainly rather than
let it read as a broken tool. Cost if wrong: the CAN Driver half of the
matrix is less interesting than hoped; recoverable by adding a third SWS
document (CanTp/CanSM both fetch fine) without touching any code.

Ruling R6: the `@req` scanner (S1.4.3) must emit a **canonical** requirement
ID, normalizing `[<release>/]<MODULE><digits>` → `SWS_<Module>_<5-digit>`
with the module casing taken from the manifest, and store the raw annotation
alongside it. Without this, tier-1 evidence joins nothing and the whole
traceability claim silently degrades to semantic search. The mapping table
lives in `project.yaml`. Cost if wrong: T1 recall suffers; T2/T3 still work,
so the feature degrades rather than breaks.

Ruling R7: implement S1.5.1 against a mocked OpenRouter client with the
cache-hit behaviour under test, and defer the live "second run embeds 0 new
chunks" run to the point where a real `OPENROUTER_API_KEY` exists. Project
convention already forbids tests hitting OpenRouter. Cost if wrong: a live
integration bug surfaces at first real ingestion instead of now.

Ruling R8: S1.1.4 is already satisfied except the README stub; do not
re-do `.gitignore` or re-commit the spec.

Ruling R9: `Requirement` carries both `bbox` and an optional `char_span`
(the plan's superset of the spec's field list). Cost if wrong: one unused
optional field.

Ruling R10: libraries named in the approved spec or plan — pymupdf,
tree-sitter + tree-sitter-c, rank-bm25, chromadb, openai, langchain,
fastapi, uvicorn, pydantic, pyyaml, pytest, ruff — are pre-approved by that
approval. Anything **not** named there stops for the human partner, per
CLAUDE.md. Cost if wrong: an unwanted dependency, visible in the lockfile
diff.

Ruling R11: S1.3.4 says to commit "3–5 frozen PDF pages" as golden
fixtures, which collides head-on with CLAUDE.md and spec §2 ("Do NOT commit
the PDFs", `data/` is gitignored) — and the AUTOSAR documents are
copyrighted, so committing page extracts is the worse half of the conflict.
Resolution: commit the **frozen parser output** for 3–5 pages (per-page text
blocks with page number and bbox, as JSON) plus the hand-labeled expected
requirements — not the PDF pages themselves. The golden extractor tests run
entirely against those snapshots, so they work on a fresh clone with no
`data/`. The parser's own mechanics (S1.3.2) are tested against a tiny
synthetic PDF generated in-test by PyMuPDF, with an opt-in test against the
real document that skips when `data/docs/` is absent. Cost if wrong: the
golden fixtures pin the parser's output format rather than its PDF input, so
a PyMuPDF upgrade that changed block segmentation would need the snapshots
regenerated deliberately — which is what "update them deliberately with
hand-verified labels" already requires.

Ruling R12: the code carries **two** requirement markers with opposite
meanings — `/* @req 4.0.3/CANIF005 */` claims implemented,
`/* !req 4.0.3/CANIF316 */` explicitly claims NOT implemented. Measured
across the five target C files: **914 `@req` vs 150 `!req`** (14% of all
annotations). Neither the plan nor the spec mentions `!req` — both describe
only "`@req` annotations". The scanner must capture the marker and carry the
claim polarity through to `CodeUnit`, and tier-1 evidence must consume only
the claimed-implemented set. Collapsing them would make the traceability
report assert implementation for requirements the original developers
documented as unimplemented — a wrong answer in the product's core output,
and one a grader could catch by reading the source. As a bonus the `!req`
set is genuine negative ground truth for validating the LLM judge in WP4.
Cost if wrong: none identified; the polarity field is additive.

Ruling R13: `chromadb` 1.5.9 pulls **`onnxruntime` 1.29.0** transitively
(no torch, no sentence-transformers — verified). Spec §3 says "no local
model dependencies (no torch)"; onnxruntime is not torch but it is a local
inference runtime, arriving as the backing for Chroma's *default* embedding
function. Accept the transitive dependency — chromadb is spec-mandated and
there is no torch-free-and-onnx-free variant — but forbid its use: every
`add`/`upsert` must pass `embeddings=` explicitly, and the collection must
be constructed so an omission fails loudly rather than silently embedding
with a local MiniLM into a different vector space. Cost if wrong: a heavier
install than the spec implies; the failure mode it guards against (mixed
embedding spaces in one collection) is silent and would corrupt retrieval,
which is why the guard is a test, not a comment.

Ruling R14 (not a ruling so much as a measured trap, recorded so it is not
rediscovered): `BM25Okapi` returns **all-zero scores on tiny corpora** —
Okapi IDF is `log((N-n+0.5)/(n+0.5))`, exactly 0 at `N=2, n=1`. Measured on
one query with a correct identifier-aware tokenizer: N=2 → idf 0.0000, all
scores 0; N=4 → idf 0.8473, top 2.905; N=8 → idf 1.6094, top 4.41. BM25
fixtures must therefore hold ≥8 documents and assert ranking order, not
absolute scores. Without this an implementer "fixes" a healthy tokenizer to
chase a zero that was never a bug — and the identifier tokenization
(`Can_Write` → `can_write`, `can`, `write`) is precisely what spec §4 puts
BM25 in the design to provide.

Ruling R15: the golden fixture set is **six** pages, not the plan's "3–5":
CAN Driver 32 (9 plain requirements), 43 (2 titled, both table-bodied, with
the source's own "Definiton" misspelling), 34+35 (`SWS_Can_00407` spanning
the page break — the `bbox is None` case, inherently two pages), 23 (§6
tracing table, must yield zero), 94 (`SWS_Can_CONSTR_*`), and CAN Interface
34 (7 plain requirements in the other ID casing). Rationale: the page-break
case cannot be expressed in one page, and both documents must appear because
`SWS_Can_*` vs `SWS_CANIF_*` casing differs — a single-document fixture set
would pass while a casing bug shipped. Cost if wrong: two extra small JSON
fixtures to maintain.

Ruling R16: the `code_units` primary key **must** include `kind` and
`line_span` (or an equivalent content-addressed unit id); D2's
`(project_id, repo_path, symbol)` is insufficient. Verified directly with
tree-sitter-c: one C file readily yields several AST units sharing a symbol
name at the same lines — `typedef struct { uint8 a; } Can_PduType;` emits
both a `type_definition` and a `struct_specifier`, and
`struct CanIf_Global {…};` followed by
`typedef struct CanIf_Global CanIf_GlobalType;` emitted **three** nodes
named `CanIf_Global`. D5 feeds exactly this C (`CanIf.c`, `CanTp.c`,
`CanSM.c`, `CanNm.c`, `Com.c` and their headers) into the table by upsert,
so the current key silently overwrites real units. My D6 brief already
assumed `(repo_path, symbol, line_span)`, so the schema is also inconsistent
with a downstream brief. This enters D2's fix loop. Cost if wrong: none —
widening a key on a table with no rows yet is free; leaving it narrow loses
code units silently, which is the worst failure mode available here.

Ruling R17: `report_runs` having a table but no CRUD is **correct and
intentional** — the brief's CRUD list omitted it deliberately because story
S4.2.1 (WP4) owns the job registry that reads and writes it. Recorded so a
later reviewer does not flag it as an oversight.

Ruling R18: **approve** the four added token groups (`--verdict-*`,
`--source-*`, `--code-*`, `--destructive-bg/-border`). shadcn's base is pure
greyscale, so verdicts and citations genuinely had no colour to use. These
are CSS custom properties in the project's own stylesheet, not a new
dependency, so the locked "Tailwind + shadcn/ui" decision is untouched.
Two properties of the proposal make it the right call rather than merely an
acceptable one: `missing` is deliberately *not* a colour (muted grey plus a
dashed glyph) with red reserved exclusively for real errors, and all four
verdicts are shape-coded so they survive greyscale, colour-blindness and a
PNG export. Cost if wrong: a palette edit in one file.

Ruling R19: report **launch is a dialog**, results live in the drawer. The
designer flagged this as a deviation from spec §7's "report drawer", but
story S5.5.1 says "launch dialog (scope + cost estimate)" in as many words,
so the plan already sanctions it. A modal is also the right shape for a
confirm-cost-before-spending step. Cost if wrong: swapping one primitive.

Ruling R20: **missing rows stay collapsed per module by default**, with the
reason distribution shown on the collapsed group and expand-on-demand. With
CAN Driver at 1 of 240, a flat matrix is ~239 consecutive `missing` rows —
unreadable, and it makes a correct result look like a failure. Collapsing
summarizes without hiding, which is the distinction that matters here. Cost
if wrong: a default flag flip.

Ruling R21: **cut the two-page PDF view** for page-break requirements.
Story S5.3.1 scopes the source pane to "pdf.js page render + bbox highlight
overlay"; a dual-page renderer is scope the plan does not carry, and the
honest cheap alternative — render the start page and mark that the
requirement continues overleaf — costs almost nothing. Revisit only if the
owner asks. Cost if wrong: page-spanning requirements read slightly worse
in v1; the extractor still stores the full text, so nothing is lost.

Ruling R22: the designer's fifth question was factual, and the answer is
**yes** — `CANIF064 → SWS_CANIF_00064` is exactly what the extractor does. I
verified it through the real manifest code path (`annotation_pattern` →
`annotation_module_map` → `annotation_id_template`) and confirmed
`SWS_CANIF_00064` is a genuine R23-11 requirement ("Shared code shall be
reentrant", cited against `SRS_BSW_00323`). The canvas's normalisation
example is accurate and needs no change.

Ruling R23: WP5 starts **out of the plan's stated WP1→…→WP5 order**, running
concurrently with the remaining WP1 backend tasks, because the owner asked
for frontend progress and the approved canvas makes it buildable. The
sequencing risk is real but bounded, and the spec's own architecture is what
bounds it: §7 and §11 require every piece of chat UI state — tool chips,
citation chips, cost lines, checkpoint restore — to come from the stored SSE
event stream. So a canned array of typed events is a *legitimate* seam, not
a throwaway mock: the renderer built against it is the same renderer the
real `POST /chat` stream drives once WP3 lands. What must not happen is the
frontend inventing endpoint shapes or parsing citations out of prose —
both are explicitly forbidden and both would be genuine rework.
Cost if wrong: if WP3's envelope drifts from spec §6, the adapter at the
seam changes and the components do not.

Ruling R24: keep the problem, **replace the solution**. D3 wrote
`ingestion/tls_chain.py` (175 lines) plus 131 lines of tests to chase the
AIA URI at runtime, including a hand-rolled bounded DER scan for one OID
TLV — which the implementer itself flagged as the part needing scrutiny.
Instead, commit the missing intermediate as a PEM and add it to an
`ssl.SSLContext` alongside certifi. I verified this end to end: the real PDF
downloads (1 451 564 bytes, `%PDF-`), `verify_mode=CERT_REQUIRED` and
`check_hostname=True` are intact, and a control request to
`expired.badssl.com` is still correctly rejected. The missing intermediate is
"Starfield Secure Certificate Authority - G2", valid **2011-05-03 →
2031-05-03**, whose root ("Starfield Root Certificate Authority - G2") is
already in certifi. Rationale: it deletes ~300 lines of security-sensitive
hand-rolled ASN.1 handling in favour of ~10 lines of stdlib, it is
deterministic and auditable in the diff, it needs no extra runtime network
fetch, and it fails loudly with an actionable message rather than silently
trusting whatever an AIA URI serves. Cost if wrong: if autosar.org changes
CA before 2031, ingestion stops with a clear error until the pinned PEM is
refreshed — a visible, deliberate act, and a public CA certificate is not a
secret, so committing it is normal.

Ruling R25: **ratify D3's text-normalization contract** and make it binding
on D4. D3 had to define what "the full requirement text" means against raw
justified PDF output, and chose: strip page furniture, drop the `▽`/`△`
markers, delete `-\n` hyphenation, collapse whitespace. It is documented in
`backend/tests/fixtures/golden/README.md`. It matches the cleaning pass I
measured 99.6%/99.7% recall with, and the hand-labeled `.expected.json`
files already encode it, so D4 has no freedom here regardless — it must
implement exactly this or the golden tests fail. D3 was right that it was
arguably D4's decision; it was made correctly, so it stands. Cost if wrong:
the contract lives in one documented place and the fixtures enforce it.

**Controller-verified the golden fixtures are real, not machine-generated.**
This was the integrity question that mattered most — a "golden" file
produced from the extractor's own output validates nothing, and the brief
forbade it. I compared `can_driver_p032.expected.json` against an extraction
I derived independently during planning, with a regex written before D3
existed: **9 requirements, identical IDs in identical order, and all 9
requirement texts byte-identical.** The fixture's own notes also read like
someone who actually looked at the page — they call out that justification
breaks lines mid-identifier (`Can_-\nSetControllerMode`, `partici-\npating`)
and that this page's requirements carry an empty `()` upstream list, which
matches my independent measurement that 94 of the CAN Driver's 240 blocks
have no upstream refs. Seven fixture pages are committed (six pages plus the
page-break pair), 6.2–11.9 KB of frozen blocks each, and no PDF anywhere.

**Carry into D4:** D3 observed that three of the four documents contain
exactly one more `⌈` than their expected requirement count. So D4 needs one
documented exclusion per document to land on 240/398/175/241 — and each
exclusion must be *named and justified* in `extraction_report.md`, never a
silent `-1`.

Ruling R26: **accept the implementer's judgment on duplicate requirement
ids** — keep the warning in the extractor and make it an error at the SQLite
insert, with the ingestion CLI (S1.5.3) exiting non-zero on any extractor
warning *after* writing the report. Its reasoning beat my framing: the
extractor cannot see the storage constraint (a composite key would make
cross-document duplicates legal), and raising inside the extractor would
suppress the very `extraction_report.md` that names which id collided.
Measured risk today is nil — 1054 extracted, 1054 unique. Cost if wrong: a
duplicate surfaces one stage later than it could, with a better diagnostic.
**Carry into D6:** the CLI must exit non-zero on extractor warnings after
writing the report.

**R26 amended (task D7).** "An error at the SQLite insert" was not
implementable and is withdrawn: `core/db.py`'s upserts are `ON CONFLICT DO
UPDATE`, which idempotent re-ingestion *requires* — a second run must
overwrite its own rows rather than fail on them. The protection is therefore
entirely in `ingestion/run.py`, and it is now two checks rather than one: the
extractor's per-document warning, plus `cross_document_duplicates()` for the
same id in two documents, which no per-document pass can see and which the
upsert would otherwise silently merge into one requirement wearing the
other's text. Both fire after the report is written, so the ordering the
ruling cared about is unchanged. The docstrings that described the old
wording were corrected in the same commit.

Ruling R27: on the **checkpoint rail**, the implementer is right about the
deviation and wrong about the mechanism. The canvas's literal per-turn
alignment genuinely breaks the rail — at 300–450 px per exchange only two
dots are reachable, and spec §11's whole requirement is that clicking a dot
scrolls *and* restores the source pane, which an unreachable dot cannot do.
So the deviation stands and the owner has been told. But
proportional-by-measurement degrades to even spacing anyway when turns are
roughly equal, while costing a `ResizeObserver`, a rAF measure loop and a
`revision={messages.length*1000 + liveEvents.length}` hack to force
remeasurement. Replaced with plain `index/(n-1)`, which also fixes the
dot-overlap finding structurally and deletes all three pieces of machinery.
Cost if wrong: dots no longer hint at exchange length — a hint the canvas
never promised and which was unreadable at real turn heights anyway.

Other Importants dispatched: the demo thread was seeded into `localStorage`
**unconditionally, including in a `live` build**, so a fixture would appear
in a real user's thread list once WP3 lands; and `Exchange.preview` is
computed for every exchange but never rendered, which also makes the prior
report's claim that `splitParagraphs` "now has a real caller" **false** — I
asked for the claim to be corrected, not just the code, since I rely on
these reports to decide what to verify.

**My unreviewed bitmask fix was reviewed and confirmed correct**, including
the implementer's hardening against `FontStyle.NotSet = -1` (which matches
every mask, so flooring negatives was right). `=== 3` was wrong in both
directions. That closes the one item I had flagged as having bypassed review.

Deferred minors: `liveEvents` not thread-scoped; the single-slot
`streamThreadRef` race (unreachable today); duplicated font-style decode
across `code-highlight.ts`/`code-highlight-client.ts`; unused `targetKey`
and `EMPTY_RENDER_STATE`; the sidebar labelling `CANIF_FIXTURE_SHA` as
coming from `project.yaml` (true only by coincidence); `aria-live` on the
whole transcript; and the fixture never exercising an out-of-order
`tool_result` or a non-null `bbox` — the last matters as soon as WP3 serves
real bboxes.

**Note on process:** the frontend agent's transcript was gone when I tried
to resume it, so per the skill's guidance I dispatched a **fresh**
implementer carrying a written findings file
(`task-F51-fix1-findings.md`) plus the existing report as its memory. Worth
recording as the reason F5.1's fix round has a different agent than its
implementation.

Task D4: fix round 1/5 (2 addressed, 0 open — commit `84b0a49`), but the
re-review surfaced **1 Important new risk in the fix diff**, which per
process joins the open list rather than being deferred. Fix round 2
dispatched.

`caption_extent()` (`context_chunker.py:198-227`) walks line-by-line until
**any** line ends in `.!?`, so a caption with no period on its own line
fused with a paragraph whose first sentence spans several physical lines
consumes the caption *and the whole paragraph* — dropping it with
`caption_remainders_kept` still 0, so the disclosure counters show nothing
anomalous. That is the exact silent-loss failure mode round 1 existed to
close, one level deeper. It does not fire on the current four documents
(empirically checked), but the corpus already grew from two documents to
four mid-project, so "a future document" is a live case, not a hypothetical.
Its docstring also understates the bound, which would mislead the next
reader.

Instructed fix: **bound the extent and bias the default toward keeping
text** — dropping is the destructive direction, so an unsure heuristic must
keep. Bound chosen from the measured caption lengths rather than taste, and
when exceeded, treat the whole residue as prose. Plus the test that was
missing: all four round-1 tests used a caption whose own first line ends in
a period, which is precisely why this slipped through.

Task D4: fix round 2/5 (1 addressed, 0 open — commit `069c8be`); re-review
dispatched. **299 passed** (was 296), recall still 38/38, requirements still
1054.

The implementer **disproved part of my instruction with measurement**, which
is the outcome I want from a fix round rather than compliance. I said bound
the extent by length, chosen from the data. It measured all 102 residues —
97 one line, **99 with no sentence terminator at all**, extents 28–176 chars
(median 43, p90 67) — and found two things: a *line* bound would be wrong
(real captions reach 8 lines at 86 chars), and **no character bound can
separate the ambiguous case from the adversarial one**, because real max 176
overlaps adversarial ~163. So a length bound cannot be the discriminator at
all.

Its replacement is structural, keyed on where the terminator lands:
(1) the caption line ends a sentence → caption is that line, fused prose
recovered; (2) no line ends a sentence → whole residue is caption, nothing
after it to lose (99 of 102 cases); (3) a *later* line ends a sentence →
ambiguous → **keep the whole residue as prose**. `MAX_CAPTION_CHARS = 200`
survives only as a guard above the observed 176 for cases 1–2. It rejected
"strip the first line only" in case 3 because that reproduces the
mid-caption fragment round 1 existed to avoid, and reports the abbreviation
mirror case now resolving toward keeping for free.

**One corpus figure moved, and it is the fix working:** context chunks
1291 → **1292**. Both case-3 residues are in `can_driver` — `Figure 7.3`
(176 chars) is now kept and becomes a chunk, and `Figure 7.5` (116 chars) is
kept but falls under the minimum and is counted in `chunks_dropped_short`
(36 → 37) instead of vanishing. Controller-verified by re-running the
extractor: `can_driver` 287 → 288, the other three unchanged, requirements
still 1054.

I also checked the disclosure claim and initially thought it was missing —
my grep was wrong, not the code. The report renders human-readable prose
rather than raw counter names, per document, and `can_driver` reads
"caption-like residues kept whole as prose because the caption's end was not
identifiable without guessing: **2**". 100 stripped + 2 kept whole = the 102
measured.

The re-review is asked to attack **case 2** specifically — the branch that
still deletes text and covers 99 of 102 real cases — and to scrutinise the
three amended round-1 tests, since amending a test to accommodate a fix is
exactly where a regression hides.

Task D4: fix round 2 re-review — **finding ADDRESSED**, but **1 Important
test-integrity defect introduced**. Round 3 dispatched (one-line fix).

The re-review confirmed the discriminator holds: the finding's verbatim
163-char residue now returns `caption_extent == 0` and is kept whole, the
docstring matches the code including the `0`-means-keep contract, and the
bound's boundary falls the safe way (`200` strips, `201` keeps). It also
confirmed the implementer's "no character bound can separate" claim holds
*a fortiori* rather than being an artifact of the previous reviewer's
specific string — an adversarial fusion can be constructed at any length.

**The defect is the good kind to catch:** the test named for the adversarial
case (`test_context_chunks.py:211-236`) padded the residue to **256
characters**, past `MAX_CAPTION_CHARS = 200` — so the length bound alone
returns 0 for it, and the re-reviewer confirmed by simulation that **the
test still passes with the entire case-3 branch deleted.** It is named for
the finding but does not exercise it, and the padding was unnecessary since
the real residue is 163 chars. Asked for the unpadded residue plus
confirmation that the test *fails* when the branch is removed — a test whose
failure mode has never been observed is not yet known to work. Coverage does
exist elsewhere (`:196` and the real 176-char `Figure 7.3` at `:170-177`),
so this is about the guard actually guarding, not an untested branch.

Ruling R28: **accept case 2's residual prose loss as a bounded limitation
rather than reopening the finding.** A period-less caption fused with prose
whose sentence continues past the residue end (page or column break, so no
terminator anywhere) is indistinguishable from the 99 legitimate case-2
residues and is still dropped. But the loss is now bounded to ≤200
characters, the exposed window is only 120–200 characters (below
`MIN_CHUNK_CHARS` it would have been dropped as short anyway), and case 2
covers 99 of 102 real captions — so making it keep would render the rule
inert. Required it to be **recorded in the report as a known bounded
limitation with the window**, so the next person does not rediscover it as a
surprise. Cost if wrong: up to 200 characters of context prose lost per
occurrence, in a corpus where zero occurrences exist today.

Folded in one Minor because the implementer is editing that constant anyway:
`MAX_CAPTION_CHARS` is compared against the **raw** extent but calibrated on
**normalized** lengths, and the 176-char residue that set the calibration is
itself a case-3 keep — so the headroom above the largest *actually stripped*
extent is unknown and under 24 chars. Either compare like with like or
recalibrate and report the real number.

Amended round-1 tests were scrutinised and passed: behavioural assertions
all survived, only the fixture was resized, and the sizing is now asserted
in-test.

Task D4: fix round 3/5 (1 addressed — commit `65eddf1`); re-review
dispatched. 299 passed (unchanged: one test repaired, none added), recall
38/38, 1054 requirements / 1292 context chunks — all unchanged, consistent
with a round that moved no behaviour.

The implementer did more than the fix asked for, in the right direction:
rather than reasoning that the repaired test would now fail on a regression,
it **built the mutant and observed it** — a round-1 line-walk with the bound
kept, precisely the variant the padded test could not distinguish. Result:
`caption_extent` → 163, `assert 163 == 0` fails, 3 tests in the file fail,
and running the chunker directly against the mutant reproduced the
behavioural half too (`chunks = 0`, paragraph survives `False`,
`caption_remainders_kept = 0`). Then restored from backup and confirmed
byte-identity before the final run.

**I verified the riskiest part of that myself**, because editing production
code to build a mutant is exactly where a serious accident hides: compared
`caption_extent`'s AST between `069c8be` and `65eddf1` with docstrings
stripped — **identical** — and `MAX_CAPTION_CHARS` unchanged at 200. No
mutant remnant survived.

The folded-in recalibration also came back better than requested. Measured
per case in raw characters: case 1 → 166 (n=1), case 2 → 29–87 (n=99),
case 3 → 117 and 177 but **returns before the bound is checked**. So the
largest extent the bound ever sees is 166, giving 200 a real headroom of 34
characters — where my premise had been that the headroom was under 24 and
unknown. The 176 figure I had been reasoning from was both normalized *and*
a case-3 keep, so it never reached the comparison at all. Value unchanged,
now justified by the quantity actually compared.

Case-2 residual documented in the `caption_extent` docstring with the
120–200 character window and why making case 2 keep would render the rule
inert, per ruling R28.

I also checked the implementer's phrase "moved no production logic" against
a 46-insertion diff to `context_chunker.py`, since that reads like a
contradiction: 33 of the added lines are prose — a per-case measurement
table plus the residual documentation I required — against a handful of
executable ones. The claim is accurate in the sense that matters, and the
file changed only because docstrings live in production files.

Task D4: **complete** (commits 3dcb980..65eddf1, review clean after 3 fix
rounds, 7 minors deferred + 1 documented bounded limitation).

Round-3 re-review: all findings addressed, no new breakage. It independently
reasoned through the mutant rather than trusting the report — confirming the
repaired test's `assert caption_extent(fused) == 0` genuinely fails at 163
under a round-1 line-walk — and independently confirmed the executable body
is byte-identical to `069c8be`, matching my own AST comparison. It also
verified the recalibration arithmetic (largest extent the bound sees = 166,
headroom 34) and judged two of the three preconditions meaningful guards
with the third harmless.

Task F5.1/F5.2: fix round 1/5 (4 addressed, 0 open — commits `743236d`,
`bef52ba`, `631989c`, `0b8faa5`); re-review dispatched. Net **−142/+47
lines**, no new dependency — the rail alone lost 154 lines with the
`ResizeObserver`, rAF loop and `revision` prop deleted, which is what ruling
R27 was for. Controller-verified: `tsc --noEmit` clean, `npm run build`
succeeded, no `package.json` change.

**Investigated the implementer's first concern and it is a non-issue.** It
reported `next dev` serving Turbopack chunks as **403** so the page never
hydrates, said it reproduced on unmodified `main`, and fell back to
`next build` + `next start` for verification. On this machine `next dev`
returns **200** for the root page and **200 for all six referenced chunks**,
with a clean server log (`✓ Ready in 242ms`). So it was an artifact of the
agent's own sandbox, not a project defect — `make dev` works. Worth
recording because the concern as written would have read as "the primary
developer workflow is broken", which it is not.

Its second concern — rail geometry now expressed in two notations
(`PAD`/`DOT` constants versus a `size-[22px]` class) — is a genuine but
cosmetic inconsistency; deferred.

Task F5.1/F5.2: **complete** (commits 19e726f..0b8faa5, review clean after 1
fix round, 8 minors deferred).

Re-review: all four findings addressed, no new breakage. Notably it verified
the report correction was made **in place** — struck through at the original
claim site with an explicit retraction — rather than merely appended at the
bottom, which is what I asked for and the kind of thing that is easy to
fudge. It also confirmed by grep that `ResizeObserver`/`requestAnimationFrame`
survive only in an explanatory comment, that `Exchange.preview`,
`previewOf` and `splitParagraphs` are gone with no orphaned imports, that
`event-reducer.ts` remains pure (type-only import), and that the rail's
click path lost no behaviour — only the measurement machinery.

Ruling R29: the re-review surfaced a **latent issue for WP3** that the fix
did not introduce and correctly left alone. Gating the demo-thread seed on
`chatSourceKind() === "canned"` prevents new writes but not stale ones: if a
`canned` build and a later `live` build ever share the same `localStorage`
origin, a demo thread seeded during the canned run persists into the live
one. Deployments are expected to differ by origin or port, so it is not a
defect today. **Carry into WP3's threads work (F3.5 / S3.5.2):** either
namespace the `localStorage` key by source kind, or have the live path
ignore or migrate canned-seeded threads on first load. Cost if wrong: a
fixture thread appears in a real user's sidebar once, recoverable by
deleting it.

Pushed through `0b8faa5`; `origin/main` current, 0 unpushed.

**The `make deps-check` guard proved itself immediately.** D5 added
`tree-sitter>=0.26.0` and `tree-sitter-c>=0.24.1` — exactly the two
pre-approved libraries, no scope creep — and `make deps-check` failed with
"dependencies.txt is stale", exit 2, before anyone had to notice. Regenerate
and commit `dependencies.txt` once D5's work lands.

Ruling R30: **lift my own `models.py` off-limits constraint for D5.** It was
my constraint, it caused both Important findings, and the correction is a
`Literal` extension plus two constants with **no migration** (the column is
TEXT). `CodeUnitKind` becomes
`["function","prototype","struct","enum","typedef","macro","file"]`.
Cost if wrong: a wider enum than strictly needed; the alternative is a
rebuild of the code index and its vectors.

Important 2 has real teeth beyond tidiness: `_Emission.declaration` is
computed correctly then **discarded into free text** (`" (declaration)"`
inside `CodeUnit.text`), so the only way to distinguish a declaration from
an implementation is string-matching prose the reranker also embeds — and
**301 of 641 `function` units are declarations**. WP4's judge decides "is
this implemented", so a requirement annotated only on a `CanIf.h` prototype
currently reads as tier-1 implemented evidence, biasing verdicts toward
`implemented` exactly where the evidence is weakest.

Third deviation — using `.git` presence rather than `git rev-parse HEAD` for
the skip check — was reviewed and **accepted** as the right call given the
zero-invocation criterion.

Folded two Minors into the same fix because they are correctness-adjacent
and in the files being edited: `union_specifier` silently mapping to
`"struct"` (do not leave a second mislabel while fixing the first), and
`text.splitlines()` → `text.split("\n")`. The latter is worth closing
permanently — `splitlines()` also breaks on `\f`, `\v`, `\x1c-\x1e`, `\x85`
and the Unicode line separators, while tree-sitter rows and
`annotations.line_offsets` count only `\n`, so a form feed (routine
page-break punctuation in old C) would **silently shift every span below
it**. The reviewer checked all 580 snapshot files: only 3 empty lwip files
differ and all are excluded, so no impact today — but a silent span shift is
the worst class of bug for a code viewer.

What the review confirmed was done properly, and *proved* rather than
asserted: `declarator_name` descends only the declarator field rather than
walking descendants (the exact trap the brief warned about); `line_span` is
verified against the fixture's actual bytes rather than a second copy of the
arithmetic; the PATH-shim zero-git proof is sound because
`subprocess_git_runner` is the only `subprocess` use with no `shell` and no
`GIT_*` fiddling, and the warm run cannot short-circuit for an unrelated
reason; annotations are unloseable by construction, since the scanner is a
raw-text sweep so damaged regions still yield them and leftovers become
file-scope units; and the C fixtures are plausibly the implementer's own
(`unsigned char` rather than `uint8`, invented CANIF700/701/ZZZ001, no
AUTOSAR banner).

Deferred minors: `read_head_sha` proving HEAD rather than the work tree; the
module-global `lru_cache`d `Parser` not thread-safe (comment it, since D6
may parallelize); `glob_to_regex` recompiling per call; unannotated
prototypes earning an embedding; and the hard-coded `PINNED_SHA` coupling
the suite to the manifest — arguably a feature since it pins ruling R4.

Task D5: fix round 1/5 (2 Important + 2 folded Minors addressed, 0 open —
commit `9895e1a`). **418 passed** (was 410), ruff clean.
Task D5: **complete** (commits 0b8faa5..9895e1a, review clean after 1 fix
round, 5 minors deferred).

The fix quantified the argument I had made qualitatively: of 1089 distinct
`@req` ids, 769 have real body evidence and **188 have their only
claimed-implemented evidence on a prototype or file-scope unit — 41 of which
join a real requirement.** So 41 requirements would have reported
implementation evidence indistinguishable from an actual function body. That
number belongs in the README's honesty section. Controller-verified by
re-running the index: all 8 kind members present, 1253 units redistributed
as `function` 340 / `prototype` 301 / `macro` 403 / `file` 56 / `typedef`
153, annotations unchanged at 1454 + 192, 188 weak-evidence ids matching.
Labels moved, nothing else did.

Two design points worth keeping: `_Emission.declaration` became a
**property derived from `kind`** on a frozen dataclass, so label and
breadcrumb cannot drift apart — the failure mode is designed out rather than
tested for; and `union` was added as its own member rather than folded into
`struct`, on the same never-repurpose-a-key-value logic that made the
primary-key timing urgent.

The re-review checked the two things that would have made this hollow:
whether the new `== FILE_UNIT_KIND == "file"` assertion is tautological (it
is not — it catches drift in either direction), and whether the amended
tests were weakened. They were **strengthened**:
`test_header_prototypes_produce_prototype_units` gained
`assert not any(u.kind == "function" for u in result.units)`. It also
confirmed the zero union count by grepping the real repository, and that no
`splitlines()` survives on any path feeding `line_span` — the remaining uses
are git-internals parsing and PDF text.

Pushed through `9895e1a`; `origin/main` current.
