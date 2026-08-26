"""The Chroma vector store: one collection per ``project_id`` (story S1.5.2).

Persisted under ``data/chroma/`` (gitignored, derived — deleting it and
re-running the ingestion CLI must rebuild it).

**The trap this module exists to close.** ``chromadb`` pulls ``onnxruntime``
as a transitive dependency, and that runtime backs Chroma's *default*
embedding function: a local all-MiniLM-L6-v2 producing **384**-dimension
vectors. This project's vectors come from OpenRouter and are **1536**-dimension
(spec §3, CLAUDE.md). If a single ``add``/``upsert`` ever omits
``embeddings=``, Chroma will quietly download the ONNX model and embed the
text itself, mixing two incompatible vector spaces in one collection — an
index that looks healthy and retrieves nonsense.

Passing ``embedding_function=None`` is **not** sufficient to prevent that, as
of chromadb 1.5.9. ``Collection._embed`` falls through from the collection's
embedding function to ``self.configuration["embedding_function"]`` and then to
the schema's default, so a collection created with ``embedding_function=None``
alone still embeds with MiniLM. Measured directly: ``add(documents=[...])``
with no embeddings raised ``Collection expecting embedding with dimension of
3, got 384`` — i.e. it had already run the local model.

What *does* work, and is therefore what :func:`open_collection` does, is
pinning it off in the stored collection **configuration** as well
(:data:`COLLECTION_CONFIGURATION`). With that, the same call raises
``"You must provide an embedding function to compute embeddings"`` — loud, at
the point of the mistake — and the pin survives reopening the store, because
it is persisted with the collection rather than supplied per client.
:func:`upsert` refuses to call Chroma without vectors on top of that, so the
guarantee does not rest on one mechanism.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaSettings

from retrieval.chunks import IndexDocument

#: Pins Chroma's embedding function off *in the persisted collection
#: configuration*. See the module docstring — this, not the
#: ``embedding_function=None`` argument, is what makes an omitted
#: ``embeddings=`` fail loudly instead of silently invoking a local MiniLM.
COLLECTION_CONFIGURATION: dict[str, Any] = {"embedding_function": None}

#: Cosine distance: OpenAI embeddings are normalized, and cosine is what the
#: retrieval pipeline's fusion (spec §4) assumes.
COLLECTION_METADATA: dict[str, Any] = {"hnsw:space": "cosine"}

#: Documents per ``upsert`` call. Chroma's own ceiling is 5461 records; a
#: smaller window keeps peak memory down for 1536-float vectors and makes a
#: failure cheap to resume.
UPSERT_BATCH_SIZE = 500


class VectorStoreError(Exception):
    """Raised when the store cannot be opened, or an upsert is malformed."""


@dataclass(frozen=True)
class VectorHit:
    """One nearest-neighbour result: which chunk, how far, at what rank."""

    id: str
    rank: int
    distance: float
    metadata: dict[str, Any]
    text: str | None = None


def open_client(path: Path) -> chromadb.ClientAPI:
    """Open (creating if needed) the persistent store at ``path``.

    Telemetry is off: this is an offline, single-user tool and phoning home
    about a graded submission's index would be indefensible.
    """
    path.mkdir(parents=True, exist_ok=True)
    return chromadb.PersistentClient(
        path=str(path),
        settings=ChromaSettings(anonymized_telemetry=False),
    )


def open_collection(client: chromadb.ClientAPI, project_id: str) -> chromadb.Collection:
    """The collection for ``project_id``, with no default embedding function.

    Idempotent: ``get_or_create`` with the same configuration on a store that
    already holds the collection returns it untouched, which is what lets the
    ingestion CLI be re-run.
    """
    try:
        return client.get_or_create_collection(
            name=project_id,
            configuration=COLLECTION_CONFIGURATION,
            embedding_function=None,
            metadata=COLLECTION_METADATA,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as VectorStoreError
        raise VectorStoreError(
            f"could not open Chroma collection {project_id!r} — {exc}"
        ) from exc


def upsert(
    collection: chromadb.Collection,
    documents: Sequence[IndexDocument],
    embeddings: Sequence[Sequence[float]],
) -> int:
    """Upsert ``documents`` with ``embeddings``; return how many were written.

    Every guard here exists because its absence is silent. A length mismatch
    or an empty vector would either raise deep inside Chroma or, worse, pair
    vectors with the wrong chunks; a varying dimension means two embedding
    spaces have been mixed. And ``embeddings=`` is always passed explicitly —
    there is no code path in this project that lets Chroma embed anything.
    """
    if len(documents) != len(embeddings):
        raise VectorStoreError(
            f"{len(documents)} document(s) but {len(embeddings)} embedding(s) — refusing "
            "to pair vectors with chunks by position"
        )
    if not documents:
        return 0

    dimensions = {len(vector) for vector in embeddings}
    if len(dimensions) != 1 or 0 in dimensions:
        raise VectorStoreError(
            f"embeddings have inconsistent or empty dimensions {sorted(dimensions)} — "
            "one collection must hold exactly one vector space"
        )

    written = 0
    for start in range(0, len(documents), UPSERT_BATCH_SIZE):
        window = documents[start : start + UPSERT_BATCH_SIZE]
        vectors = [list(vector) for vector in embeddings[start : start + UPSERT_BATCH_SIZE]]
        try:
            collection.upsert(
                ids=[document.id for document in window],
                embeddings=vectors,
                documents=[document.text for document in window],
                metadatas=[dict(document.metadata) for document in window],
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as VectorStoreError
            raise VectorStoreError(
                f"Chroma upsert failed for {len(window)} document(s) starting at "
                f"{window[0].id!r} — {exc}"
            ) from exc
        written += len(window)
    return written


def query(
    collection: chromadb.Collection,
    embedding: Sequence[float],
    *,
    limit: int = 5,
    where: dict[str, Any] | None = None,
) -> list[VectorHit]:
    """Nearest neighbours of ``embedding``, closest first.

    A read-only convenience for the ingestion CLI's smoke query and for
    WP2's dense stage to build on; it holds no state of its own.
    """
    if limit < 1:
        return []
    result = collection.query(
        query_embeddings=[list(embedding)],
        n_results=limit,
        where=where,
        include=["metadatas", "documents", "distances"],
    )
    ids = (result.get("ids") or [[]])[0]
    distances = (result.get("distances") or [[]])[0]
    metadatas = (result.get("metadatas") or [[]])[0]
    texts = (result.get("documents") or [[]])[0]
    hits: list[VectorHit] = []
    for index, chunk_id in enumerate(ids):
        hits.append(
            VectorHit(
                id=chunk_id,
                rank=index + 1,
                distance=float(distances[index]) if index < len(distances) else float("nan"),
                metadata=dict(metadatas[index] or {}) if index < len(metadatas) else {},
                text=texts[index] if index < len(texts) else None,
            )
        )
    return hits
