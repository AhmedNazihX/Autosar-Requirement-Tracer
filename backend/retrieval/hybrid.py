"""Reciprocal Rank Fusion, joining BM25 and dense rankings (story S2.2.2).

Spec §4's hybrid stage runs the two indexes in parallel and fuses them. Fusion
by *rank* rather than by score is the whole trick: BM25 returns Okapi scores on
an unbounded scale that depends on corpus statistics, Chroma returns cosine
distances in [0, 2], and there is no principled way to add those two numbers
together. Ranks are comparable by construction.

The formula is Cormack's::

    score(d) = Σ  1 / (k + rank(d, list))
             lists

with ``k = 60``, the constant from the original paper and the de-facto default.
``k`` damps the advantage of rank 1 so that a document both indexes rank
*second* can beat one that a single index ranks first — which is exactly the
behaviour wanted here, because agreement between a lexical and a semantic
retriever is stronger evidence than one retriever's confidence.

**The two indexes deliberately do not cover the same chunks.** WP1 excludes 217
unannotated header prototypes from the vector index (``ingestion/run.py``)
while BM25 is built from every row in SQLite, so a chunk can appear in the
lexical ranking and be structurally unreachable in the dense one. That is
intended, not a bug to normalise away: an exact-symbol query should still find
a declaration, and a chunk found by one index simply scores lower than one
found by both, which is the correct treatment of weaker evidence.

Fusion is pure arithmetic over ranked id lists, which is what makes story
S2.2.2's acceptance criterion — a unit test on synthetic rankings — meaningful,
and what lets story S7.2's RAGAS baseline swap the pipeline for a naive
top-k without touching this.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from retrieval.bm25 import Bm25Hit
from retrieval.dense import DenseHit

#: Cormack's constant. Raising it flattens the curve (rank 1 matters less);
#: lowering it sharpens it. 60 is the published default and is not tuned here —
#: story S7.2's evaluation is where a change to it would have to be justified.
DEFAULT_RRF_K = 60

#: Names used for the two rankings. Constants rather than string literals at
#: the call sites so the provenance recorded in :attr:`FusedHit.ranks` — which
#: the stage instrumentation and the RAGAS baseline read — cannot drift.
BM25_SOURCE = "bm25"
DENSE_SOURCE = "dense"


@dataclass(frozen=True)
class FusedHit:
    """One chunk's fused standing.

    ``ranks`` records where each contributing index placed it. That is kept
    rather than discarded because it is the cheapest possible answer to "why
    did this come back?" — for the stage log (story S2.6.1), for debugging a
    bad answer, and for reporting how much the hybrid actually adds over
    either index alone (story S7.2).
    """

    id: str
    score: float
    rank: int
    ranks: dict[str, int]


def reciprocal_rank_fusion(
    rankings: Mapping[str, Sequence[str]],
    *,
    k: int = DEFAULT_RRF_K,
    limit: int | None = None,
) -> list[FusedHit]:
    """Fuse named ranked id lists into one ranking, best first.

    Each value of ``rankings`` is ids in rank order, best first. A duplicate id
    within one list scores once, at its best rank — multi-query expansion
    (story S2.3.1) merges several rewrites' results and duplicates are the
    normal case, not an error.

    ``limit`` truncates *after* fusion, so a chunk that only ranks well once
    the two indexes are combined is not dropped before it can.
    """
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k} — rank 1 would otherwise dominate")

    best_ranks: dict[str, dict[str, int]] = {}
    for source, ids in rankings.items():
        for position, chunk_id in enumerate(ids, start=1):
            per_source = best_ranks.setdefault(chunk_id, {})
            existing = per_source.get(source)
            if existing is None or position < existing:
                per_source[source] = position

    scored = [
        (sum(1.0 / (k + rank) for rank in per_source.values()), chunk_id, per_source)
        for chunk_id, per_source in best_ranks.items()
    ]
    # Descending score, then by id. The id tiebreak is what makes two runs over
    # the same input produce byte-identical output — dict order would not,
    # and a non-reproducible ranking cannot be evaluated.
    scored.sort(key=lambda item: (-item[0], item[1]))

    if limit is not None:
        scored = scored[:limit]
    return [
        FusedHit(id=chunk_id, score=score, rank=rank, ranks=dict(per_source))
        for rank, (score, chunk_id, per_source) in enumerate(scored, start=1)
    ]


def fuse(
    *,
    bm25_hits: Sequence[Bm25Hit] = (),
    dense_hits: Sequence[DenseHit] = (),
    k: int = DEFAULT_RRF_K,
    limit: int | None = None,
) -> list[FusedHit]:
    """:func:`reciprocal_rank_fusion` over one BM25 and one dense result.

    A thin adapter, kept separate so the arithmetic above never has to know
    what a hit object looks like.
    """
    return reciprocal_rank_fusion(
        {
            BM25_SOURCE: [hit.record.id for hit in bm25_hits],
            DENSE_SOURCE: [hit.id for hit in dense_hits],
        },
        k=k,
        limit=limit,
    )
