"""Tests for the dense stage and Reciprocal Rank Fusion (stories S2.2.1, S2.2.2).

BM25's half of the hybrid pair is already covered in ``test_index.py``; this
module covers the other half and the arithmetic that joins them.

The dense fixture uses a **toy embedding with real semantics** rather than
arbitrary vectors: each dimension counts one concept word, so "how does the
driver report bus-off" genuinely lands nearest the bus-off requirement. An
arbitrary-vector fixture would assert that Chroma sorts floats, which is not
the property worth protecting.

Fusion is tested on synthetic rankings with hand-computed scores. The point of
RRF is that agreement between two rankings beats a single strong opinion, and
that is asserted directly rather than inferred from an ordering.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from core import db
from core.embeddings import EmbeddingBatch, EmbeddingClient, EmbeddingError
from retrieval import dense, hybrid, vector_store
from retrieval.chunks import IndexDocument

MODEL = "test/embedding-v1"

#: One dimension per concept word. A query that mentions a concept lands near
#: the documents that mention it, which is the behaviour the dense stage is
#: *for* — catching paraphrase that BM25's exact tokens miss.
CONCEPTS = ("busoff", "transmit", "baudrate", "init")


def embed_text(text: str) -> list[float]:
    """A deterministic, unit-length bag-of-concepts vector."""
    lowered = text.lower().replace("bus-off", "busoff").replace("bus off", "busoff")
    raw = [float(lowered.count(concept)) for concept in CONCEPTS]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


def transport(texts, model_id: str) -> EmbeddingBatch:
    return EmbeddingBatch(
        vectors=[embed_text(text) for text in texts],
        prompt_tokens=len(texts),
        total_tokens=len(texts),
        cost_usd=1e-7 * len(texts),
    )


CORPUS = (
    ("req:SWS_Can_00020", "The driver shall notify the upper layer on bus-off."),
    ("req:SWS_Can_00021", "Can_Write shall transmit the PDU to the hardware."),
    ("req:SWS_Can_00022", "Can_SetBaudrate shall change the baudrate at runtime."),
    ("req:SWS_Can_00023", "Can_Init shall initialise the module."),
)


@pytest.fixture
def collection(tmp_path: Path):
    client = vector_store.open_client(tmp_path / "chroma")
    handle = vector_store.open_collection(client, "autosar-can")
    documents = [
        IndexDocument(id=chunk_id, text=text, metadata={"doc_type": "requirement"})
        for chunk_id, text in CORPUS
    ]
    vector_store.upsert(handle, documents, [embed_text(text) for _, text in CORPUS])
    return handle


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "cache.db")
    db.migrate(connection)
    yield connection
    connection.close()


def client(**kwargs) -> EmbeddingClient:
    return EmbeddingClient(model_id=MODEL, transport=kwargs.pop("transport", transport), **kwargs)


# --------------------------------------------------------------------------
# the dense stage (story S2.2.1)
# --------------------------------------------------------------------------


def test_a_paraphrased_query_finds_the_requirement_it_describes(collection):
    """The reason dense retrieval is in the pipeline at all (spec §4)."""
    result = dense.search(collection, client(), "how does the driver report bus off?", limit=2)

    assert result.hits[0].id == "req:SWS_Can_00020"
    assert result.hits[0].rank == 1


def test_hits_come_back_ranked_from_one(collection):
    result = dense.search(collection, client(), "baudrate", limit=3)
    assert [hit.rank for hit in result.hits] == [1, 2, 3]
    assert result.hits[0].id == "req:SWS_Can_00022"


def test_the_limit_is_respected(collection):
    assert len(dense.search(collection, client(), "transmit", limit=1).hits) == 1


def test_a_metadata_filter_is_passed_through(collection):
    """Self-query filters (story S2.4.1) arrive here as a Chroma ``where``."""
    matched = dense.search(collection, client(), "transmit", where={"doc_type": "requirement"})
    assert matched.hits

    missed = dense.search(collection, client(), "transmit", where={"doc_type": "code"})
    assert missed.hits == []


def test_the_hit_carries_its_metadata_and_text(collection):
    hit = dense.search(collection, client(), "bus off", limit=1).hits[0]
    assert hit.metadata["doc_type"] == "requirement"
    assert "bus-off" in (hit.text or "")
    assert hit.distance >= 0.0


def test_an_empty_query_costs_nothing(collection):
    """Embedding whitespace is a paid-for call that can only return noise."""
    calls: list[list[str]] = []

    def counting(texts, model_id: str) -> EmbeddingBatch:
        calls.append(list(texts))
        return transport(texts, model_id)

    result = dense.search(collection, client(transport=counting), "   ", limit=3)

    assert result.hits == []
    assert calls == [], "no embedding request may go out"
    assert result.usage.calls == 0


def test_the_query_embedding_is_cached(collection, conn):
    """A repeated query — a retry, or one rewrite echoing another — is free."""
    calls: list[list[str]] = []

    def counting(texts, model_id: str) -> EmbeddingBatch:
        calls.append(list(texts))
        return transport(texts, model_id)

    engine = client(transport=counting)
    first = dense.search(collection, engine, "bus off", conn=conn, limit=2)
    second = dense.search(collection, engine, "bus off", conn=conn, limit=2)

    assert len(calls) == 1, "the second search must not embed again"
    assert first.usage.calls == 1
    assert second.usage.calls == 0, "a cache hit makes no request and costs nothing"
    assert second.usage.cost_usd == 0.0
    assert [hit.id for hit in first.hits] == [hit.id for hit in second.hits]


def test_usage_reports_what_the_query_embedding_cost(collection):
    result = dense.search(collection, client(), "bus off", limit=1)
    assert result.usage.calls == 1
    assert result.usage.cost_usd == pytest.approx(1e-7)


def test_an_embedding_failure_is_not_swallowed(collection):
    def failing(texts, model_id: str) -> EmbeddingBatch:
        raise EmbeddingError("HTTP 401 bad key")

    with pytest.raises(EmbeddingError, match="401"):
        dense.search(collection, client(transport=failing), "bus off")


def test_dense_search_is_pure(collection):
    engine = client()
    first = [hit.id for hit in dense.search(collection, engine, "bus off", limit=4).hits]
    dense.search(collection, engine, "baudrate", limit=4)
    second = [hit.id for hit in dense.search(collection, engine, "bus off", limit=4).hits]
    assert first == second


# --------------------------------------------------------------------------
# Reciprocal Rank Fusion (story S2.2.2) — synthetic rankings, exact arithmetic
# --------------------------------------------------------------------------


def test_the_fusion_score_is_the_sum_of_reciprocal_ranks():
    """Hand-computed, so a changed constant or an off-by-one cannot hide."""
    fused = hybrid.reciprocal_rank_fusion({"bm25": ["a"], "dense": ["a"]}, k=60)

    assert len(fused) == 1
    assert fused[0].score == pytest.approx(2 / 61)


def test_ranks_are_one_based():
    fused = hybrid.reciprocal_rank_fusion({"bm25": ["a", "b"]}, k=60)
    assert fused[0].score == pytest.approx(1 / 61)
    assert fused[1].score == pytest.approx(1 / 62)


def test_agreement_between_two_rankings_beats_one_strong_opinion():
    """The whole point of RRF, asserted directly rather than via an ordering."""
    fused = hybrid.reciprocal_rank_fusion(
        {
            "bm25": ["strong", "agreed"],
            "dense": ["agreed", "other"],
        },
        k=60,
    )

    assert [hit.id for hit in fused][0] == "agreed"
    scores = {hit.id: hit.score for hit in fused}
    assert scores["agreed"] == pytest.approx(1 / 62 + 1 / 61)
    assert scores["strong"] == pytest.approx(1 / 61)


def test_a_document_found_by_only_one_index_still_scores():
    """BM25 indexes 217 unannotated prototypes that Chroma deliberately omits."""
    fused = hybrid.reciprocal_rank_fusion({"bm25": ["only-bm25"], "dense": ["only-dense"]})
    assert {hit.id for hit in fused} == {"only-bm25", "only-dense"}


def test_the_fused_hit_records_which_index_found_it_and_where():
    """Stage instrumentation (story S2.6.1) and the RAGAS baseline need this."""
    fused = hybrid.reciprocal_rank_fusion(
        {"bm25": ["x", "y"], "dense": ["y"]},
    )
    by_id = {hit.id: hit for hit in fused}

    assert by_id["y"].ranks == {"bm25": 2, "dense": 1}
    assert by_id["x"].ranks == {"bm25": 1}


def test_output_ranks_are_dense_and_one_based():
    fused = hybrid.reciprocal_rank_fusion({"bm25": ["a", "b", "c"]})
    assert [hit.rank for hit in fused] == [1, 2, 3]


def test_equal_scores_break_ties_deterministically():
    """Two runs over the same input must produce the same order, always."""
    rankings = {"bm25": ["b", "a"], "dense": ["a", "b"]}
    first = [hit.id for hit in hybrid.reciprocal_rank_fusion(rankings)]
    second = [hit.id for hit in hybrid.reciprocal_rank_fusion(rankings)]

    assert first == second
    assert first == ["a", "b"], "ties fall back to the id, so the order is reproducible"


def test_a_larger_k_flattens_the_advantage_of_rank_one():
    small = hybrid.reciprocal_rank_fusion({"bm25": ["a", "b"]}, k=1)
    large = hybrid.reciprocal_rank_fusion({"bm25": ["a", "b"]}, k=1000)

    small_gap = small[0].score - small[1].score
    large_gap = large[0].score - large[1].score
    assert small_gap > large_gap


def test_the_limit_truncates_after_fusion_not_before():
    fused = hybrid.reciprocal_rank_fusion(
        {"bm25": ["a", "b", "c"], "dense": ["c", "b", "a"]}, limit=2
    )
    assert len(fused) == 2


def test_fusing_nothing_returns_nothing():
    assert hybrid.reciprocal_rank_fusion({}) == []
    assert hybrid.reciprocal_rank_fusion({"bm25": [], "dense": []}) == []


def test_a_duplicate_id_inside_one_ranking_counts_once_at_its_best_rank():
    """Multi-query expansion merges three rewrites; duplicates are expected."""
    fused = hybrid.reciprocal_rank_fusion({"bm25": ["a", "b", "a"]}, k=60)

    assert [hit.id for hit in fused] == ["a", "b"], "'a' appears once, not twice"
    scores = {hit.id: hit.score for hit in fused}
    assert scores["a"] == pytest.approx(1 / 61), "scored at its better rank, not summed"


def test_a_zero_or_negative_k_is_refused():
    """``k=0`` makes rank 1 score 1.0 and divides by zero conceptually."""
    with pytest.raises(ValueError, match="k"):
        hybrid.reciprocal_rank_fusion({"bm25": ["a"]}, k=0)


# --------------------------------------------------------------------------
# joining the two indexes
# --------------------------------------------------------------------------


def test_fuse_takes_bm25_and_dense_hits_and_names_the_sources(collection):
    from retrieval import bm25

    index = bm25.build_index(
        [
            bm25.Bm25Record(id=chunk_id, text=text, doc_type="requirement", source="can_driver")
            for chunk_id, text in CORPUS
        ]
    )
    lexical = bm25.search(index, "Can_SetBaudrate", limit=4)
    vectors = dense.search(collection, client(), "baudrate", limit=4).hits

    fused = hybrid.fuse(bm25_hits=lexical, dense_hits=vectors)

    assert fused[0].id == "req:SWS_Can_00022"
    assert set(fused[0].ranks) == {"bm25", "dense"}, "both indexes agreed on it"
