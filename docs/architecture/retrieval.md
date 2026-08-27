# Retrieval: what runs when, and why

How ReqTrace decides between an exact lookup and a search, and what "hybrid"
actually does once a search starts.

Written 2026-08-27, against the code as merged after WP5. Every number here is
a named constant or a line in the source; where the two ever disagree, **the
code is right and this file is stale**. The authorities are
`docs/specs/2026-08-26-reqtrace-design.md` §3–§4 for the design and
`backend/retrieval/` for the behaviour.

---

## The short answer

**We never choose between BM25 and dense retrieval.** Both run, on every query,
and their rankings are fused. `_retrieve_fuse_hydrate`
(`backend/retrieval/pipeline.py`) calls `bm25.search(...)` and
`dense.search(...)` unconditionally inside the same loop — there is no branch
that picks one.

The choosing happens a level up: **whether a search runs at all.**

---

## 1. The routing decision

| Input | Path | What it costs |
|---|---|---|
| A requirement **id** — `SWS_CANIF_00023`, `sws can 11`, `CANIF-23` | `lookup_requirement` → fingerprint match in SQLite (`retrieval/lookup.py`) | nothing; exact |
| An exact **symbol** — `search_code(symbol="CanIf_Transmit")` | short-circuits to `db.list_code_units_by_symbol`, an indexed SQL lookup | nothing; exact |
| Anything **descriptive** — "how does the driver report bus-off?" | the hybrid pipeline | embeddings + 2–3 LLM calls |

The first two never touch either index, and that is a deliberate, locked
decision (spec §3): **an id is resolved exactly or not at all.** A
semantically-retrieved neighbour of `SWS_CANIF_00023` is indistinguishable from
a correct hit once it is quoted in prose, and the whole product claim is that
answers are source-linked.

The symbol short-circuit returns definitions before declarations
(`_KIND_PRIORITY` in `pipeline.py`), because a header prototype is not an
implementation and the evidence engine must see the stronger candidate first.

---

## 2. What each index is for

| | BM25 (`retrieval/bm25.py`) | Dense (`retrieval/dense.py`) |
|---|---|---|
| Finds | exact tokens — `CanIf_Transmit`, `SWS_CANIF_00023`, `BUSOFF` | paraphrase — "reports a bus fault" → the bus-off requirement |
| Built from | **every** row in SQLite, at startup, in memory | Chroma, one collection per project |
| Coverage | all chunks | **excludes 217 unannotated header prototypes** (`ingestion/run.py`) |

That coverage difference is intentional, not a bug to normalise away. A chunk
can appear in the lexical ranking and be structurally unreachable in the dense
one; it then simply scores lower than a chunk both indexes found, which is the
correct treatment of weaker evidence.

**Why not just use one?** A requirement id is a token BM25 nails and an
embedding blurs. A behavioural question shares no vocabulary with the
requirement that answers it. Neither retriever covers both cases, so the
system runs both and lets fusion decide.

---

## 3. Fusion: by rank, never by score

`retrieval/hybrid.py` implements Cormack's Reciprocal Rank Fusion:

```
score(d) = Σ  1 / (k + rank(d, list))
         lists
```

with **`k = 60`** (`DEFAULT_RRF_K`), the constant from the original paper.

Fusing by *rank* rather than by score is the whole trick. BM25 returns Okapi
scores on an unbounded scale that depends on corpus statistics; Chroma returns
cosine distances in `[0, 2]`. There is no principled way to add those two
numbers. Ranks are comparable by construction.

`k` damps the advantage of rank 1, so **a chunk both indexes rank second beats
one that a single index ranks first**. That is the behaviour we want:
agreement between a lexical and a semantic retriever is stronger evidence than
one retriever's confidence.

`k` is not tuned. Changing it is a change that story S7.2's evaluation would
have to justify.

---

## 4. The full pipeline, and the numbers

`search_requirements` runs all five stages of spec §4:

```
expand → self-query filter → BM25 ∥ dense → RRF → hydrate → LLM rerank
```

| Stage | Constant | Value |
|---|---|---|
| Query rewrites | `translate.DEFAULT_REWRITE_COUNT` | 3 (so 4 queries with the original) |
| Candidates per (index, query) | `pipeline.DEFAULT_PER_QUERY_LIMIT` | 10 |
| Rankings fused | 4 queries × 2 indexes | **8** |
| Fused candidates | `pipeline.DEFAULT_FUSE_LIMIT` | 20 |
| Returned | `pipeline.DEFAULT_TOP_N` | 5 |

So one question becomes eight rankings of ten, fused to twenty, reranked to
five.

**Hydration goes to SQLite, not to index metadata.** A fused hit is only a
chunk id; the record behind it is fetched from the source of truth, so a
result is a real `Requirement` or `CodeUnit` regardless of which index found
it and regardless of what that index happened to store.

**Filters constrain both sides.** The self-query stage extracts a filter
(`module=CanIf`, a section, a doc type) and it is applied twice — as a Chroma
`where` for the dense search and as an in-process predicate for BM25
(`pipeline.py`, the `conditions=` and `bm25_predicate=` arguments). A filter
that narrowed only one index would silently bias fusion toward the other.

---

## 5. The pipeline is shorter for some callers

Not every caller wants all five stages, and the differences are cost decisions:

| Caller | Expansion | Self-query | Rerank | Why |
|---|---|---|---|---|
| `search_requirements` | yes | yes | yes | the full advanced-RAG path |
| `search_code` | **no** | **no** | yes | code queries are symbol-shaped, which BM25 already handles best; rewriting buys cost not recall, and code has no section metadata to filter on |
| Evidence engine, tier 2 | no | no | **no** | the judge reads every candidate anyway, so an LLM call to reorder them buys nothing — and over a 398-row report it would double the model calls |

---

## 6. Degradation

**Nothing in the pipeline throws because a model failed.** Each LLM stage
degrades on its own terms:

- expansion → falls back to the original query
- filter extraction → falls back to no filter
- rerank → falls back to fusion order

A search therefore survives all three being down, which is asserted end to end
in the tests because it decides whether the app is usable on a bad day.

---

## 7. Seeing it per query

Every `search_requirements` call carries a stage log to the UI, rendered on the
tool chip:

```
multi-query: 3 rewrite(s) · self-query: module=CanIf · hybrid search: 4 query/ies
· 8 ranking(s) · RRF fusion: 20 candidate(s) · k=60 · LLM rerank: 20 → 5
```

Each result also records `found_by` — which index found it and at what rank,
collapsed across rewrites. That is the cheapest possible answer to "why did
this come back?".

---

## 8. What is not yet answered

Which of these stages earns its cost is an **open, measurable question**, not a
settled one. The pipeline takes `rewrites=0`, `extract_filters=False` and
`use_rerank=False`, which reduce it to exactly the naive single-query top-k
baseline — so the comparison needs no second code path.

Running that comparison is story S7.2 (RAGAS: faithfulness, answer relevancy,
context precision/recall, full pipeline vs. baseline). Until it runs, treat the
stage list as a design intent that has been *built and instrumented* but not
yet *shown* to beat the simpler thing.

---

## Related

- `docs/specs/2026-08-26-reqtrace-design.md` §3 architecture, §4 the pipeline
- `docs/findings/2026-08-26-corpus-and-toolchain-findings.md` — C4 (BM25 scores
  zero on a tiny corpus), C3 (never let Chroma use a default embedding
  function), A5 (what the corpus actually joins)
- `backend/retrieval/` — `pipeline.py` assembles it; `bm25.py`, `dense.py`,
  `hybrid.py`, `translate.py`, `self_query.py`, `rerank.py`, `lookup.py` are the
  stages
