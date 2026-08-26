"""The dense half of hybrid search (story S2.2.1).

BM25 (:mod:`retrieval.bm25`) catches the exact tokens this corpus is full of —
``Can_Write``, ``CAN_E_PARAM_POINTER``, ``ST[11]``. This module catches what
BM25 cannot: a question phrased in the user's vocabulary rather than the
specification's. "How does the driver report bus-off?" shares almost no tokens
with the requirement that answers it.

:func:`search` is a pure function of its arguments — the collection, the
embedding client and the query all come in, nothing is held between calls, and
the same inputs always produce the same ranking. Story S2.6.1 requires each
pipeline stage to be independently testable, and a module-level index or client
would make that impossible.

Two things it is careful about:

* **A blank query is not embedded.** Whitespace cannot retrieve anything
  useful, and embedding it is a paid-for call that returns noise. Multi-query
  expansion (story S2.3.1) can produce an empty rewrite, so this is a real path
  rather than a defensive flourish.
* **The query embedding goes through the cache.** Passing ``conn`` makes a
  repeated query free, which matters because expansion fans one question into
  several rewrites and rewrites repeat across turns of a conversation.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

import chromadb

from core.embeddings import EmbeddingClient, EmbeddingResult, EmbeddingUsage
from retrieval import vector_store

#: Candidates the dense side contributes before fusion. Spec §4 fuses to a
#: top-20 for reranking, so each index needs to offer more than 20 between
#: them; 10 per index per rewrite is the design's shape.
DEFAULT_LIMIT = 10


@dataclass(frozen=True)
class DenseHit:
    """One nearest-neighbour result, at its 1-based rank.

    ``id`` is the shared chunk id from :mod:`retrieval.chunks`, so a dense hit
    and a BM25 hit for the same chunk fuse (:mod:`retrieval.hybrid`).
    """

    id: str
    rank: int
    distance: float
    metadata: dict[str, Any]
    text: str | None = None


@dataclass(frozen=True)
class DenseResult:
    """The ranking, plus what embedding the query cost.

    Usage is returned rather than logged because the per-message cost line
    (story S5.2.4) has to include the retrieval side, not just the chat model.
    """

    hits: list[DenseHit]
    usage: EmbeddingUsage


def embed_query(
    client: EmbeddingClient,
    query: str,
    conn: sqlite3.Connection | None = None,
) -> EmbeddingResult:
    """Embed one query string, through the content-hash cache when given a ``conn``.

    Tagged ``purpose="embed"`` by the client; the caller distinguishes query
    embeddings from ingestion ones by where the usage is reported, not by the
    tag, since both are the same model and the same cache.
    """
    return client.embed([query], conn)


def search(
    collection: chromadb.Collection,
    client: EmbeddingClient,
    query: str,
    *,
    conn: sqlite3.Connection | None = None,
    limit: int = DEFAULT_LIMIT,
    where: dict[str, Any] | None = None,
) -> DenseResult:
    """Top ``limit`` nearest chunks to ``query``, closest first.

    ``where`` is a Chroma metadata filter — the form the self-query stage's
    typed filters (story S2.4.1) are applied in. An
    :class:`~core.embeddings.EmbeddingError` propagates: a retrieval stage that
    silently returned nothing when the embedding service was down would look
    like a corpus with no answer.
    """
    if not query.strip() or limit < 1:
        return DenseResult(hits=[], usage=EmbeddingUsage())

    embedded = embed_query(client, query, conn)
    hits = vector_store.query(collection, embedded.vectors[0], limit=limit, where=where)
    return DenseResult(hits=[_hit(hit) for hit in hits], usage=embedded.usage)


def _hit(hit: vector_store.VectorHit) -> DenseHit:
    return DenseHit(
        id=hit.id,
        rank=hit.rank,
        distance=hit.distance,
        metadata=dict(hit.metadata),
        text=hit.text,
    )
