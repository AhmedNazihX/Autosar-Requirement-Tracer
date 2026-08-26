"""What the API holds at startup, and what it builds per request.

The split matters and it is not arbitrary — it comes from what is safe to
share:

**Held for the process' lifetime** (``AppState``): the manifest, the SQLite
connection pool, the Chroma collection, the BM25 index and the per-document
page counts. The index is immutable, the Chroma client is thread-safe, and the
pool is thread-safe *by construction* (``core.db.pooled`` — one connection per
thread, see finding C10 and ``tests/test_db_threads.py``).

**Built per request**: the retrieval ``Engine``, the tool ``SideChannel``, the
chat model and its cost sink. These are per-turn state, and sharing any of them
would mix two users' answers: a ``SideChannel`` keyed by tool-call id would
serve one request's citations to another, and a ``CostSink`` would bill one
turn for another's tokens.

Startup never fails. A fresh clone has no ``data/`` at all, and the API still
has to come up so ``GET /setup/status`` can say what is missing (story S3.6.1)
— which it cannot do from a process that died. So every load path here degrades
to a recorded reason, exactly as :mod:`retrieval.startup` already does for the
BM25 index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request

from core import db, embeddings, llm, paths
from core.db import ConnectionPool
from core.manifest import ManifestError, ProjectManifest, load_manifest
from core.usage_sniffer import CostSink, sniffing_client
from retrieval import bm25, vector_store
from retrieval.pipeline import Engine


@dataclass
class AppState:
    """Process-lifetime state. Every field is safe to share across requests."""

    manifest: ProjectManifest | None = None
    pool: ConnectionPool | None = None
    collection: Any | None = None
    bm25_index: bm25.Bm25Index = field(default_factory=lambda: bm25.build_index([]))
    records: int = 0
    page_counts: dict[str, int] = field(default_factory=dict)
    #: Set when the corpus could not be loaded. The API still serves.
    error: str | None = None

    @property
    def ready(self) -> bool:
        """True when there is a corpus to answer from."""
        return self.error is None and self.records > 0 and self.manifest is not None

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close_all()


def load_state(manifest_path: str | Path) -> AppState:
    """Build :class:`AppState`, recording rather than raising on any failure."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return AppState(error=f"could not read the project manifest: {exc}")

    db_path = paths.db_path(manifest)
    if not db_path.is_file():
        return AppState(
            manifest=manifest,
            error=(
                f"no index at {db_path} — run 'uv run python -m ingestion.run "
                "../projects/autosar-can/project.yaml' to build it"
            ),
        )

    try:
        pool = db.pooled(db_path)
        db.migrate(pool)
        index = bm25.build_from_sqlite(pool, manifest)
        page_counts = db.page_counts(pool, manifest.project_id)
        collection = vector_store.open_collection(
            vector_store.open_client(paths.chroma_dir(manifest)), manifest.project_id
        )
    except Exception as exc:  # noqa: BLE001 - startup must not kill the process
        return AppState(
            manifest=manifest,
            error=(
                f"could not open the index at {db_path} ({type(exc).__name__}: {exc}) — "
                "if ingestion is running, wait for it to finish and restart"
            ),
        )

    return AppState(
        manifest=manifest,
        pool=pool,
        collection=collection,
        bm25_index=index,
        records=len(index),
        page_counts=page_counts,
    )


def state_of(request: Request) -> AppState:
    """The :class:`AppState` on this app. Never ``None``."""
    return request.app.state.reqtrace


@dataclass(frozen=True)
class TurnDeps:
    """Per-request objects for one chat turn.

    ``titler`` is here rather than built where it is used so that the whole
    turn — retrieval, chat, and the auto-title call — has exactly one seam a
    test can substitute. Building it at the call site would have made the
    auto-title the one part of a turn that reached the network during tests,
    which is precisely what happened before it moved here.
    """

    engine: Engine
    chat: llm.Llm
    cost_sink: CostSink
    titler: llm.Llm | None = None
    #: The tool-less judge behind ``check_implementation`` (spec §8). Built
    #: here for the same reason ``titler`` is: it is the turn's one seam a
    #: test can substitute, and building it at the call site would put a
    #: network call inside the tool body.
    judge: llm.Llm | None = None


def build_engine(state: AppState) -> Engine:
    """A retrieval :class:`~retrieval.pipeline.Engine` for one request.

    Cheap: everything expensive is already on ``state``. The Engine itself is a
    per-request value because it carries the request's own model handles.
    """
    if state.manifest is None or state.pool is None or state.collection is None:
        raise RuntimeError("the corpus is not loaded; check GET /setup/status")
    return Engine(
        manifest=state.manifest,
        conn=state.pool,
        collection=state.collection,
        bm25_index=state.bm25_index,
        embeddings=embeddings.client_for(state.manifest),
        translate_llm=llm.chat_model(state.manifest, "translate"),
        rerank_llm=llm.chat_model(state.manifest, "rerank"),
    )


def build_turn(state: AppState) -> TurnDeps:
    """Everything one chat turn needs, freshly built.

    The chat model gets a *sniffing* HTTP client because LangChain drops
    OpenRouter's ``usage.cost`` on the streaming path (see
    :mod:`core.usage_sniffer`); the other models are non-streaming and report
    their cost normally, so sniffing them too would double-count.
    """
    engine = build_engine(state)
    sink = CostSink()
    chat = llm.chat_model(
        state.manifest,
        "chat",
        http_client=sniffing_client(sink),
    )
    return TurnDeps(
        engine=engine,
        chat=chat,
        cost_sink=sink,
        titler=llm.chat_model(state.manifest, "title"),
        judge=llm.chat_model(state.manifest, "judge"),
    )
