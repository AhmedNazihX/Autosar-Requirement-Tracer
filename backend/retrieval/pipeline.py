"""The assembled retrieval pipeline (story S2.6.1) — the two search tools.

Spec §4 as one callable, with every stage instrumented:

    expand → self-query filter → BM25 ∥ dense → RRF → hydrate → LLM rerank

:func:`search_requirements` runs all of it. :func:`search_code` runs a shorter
version, for reasons given at that function.

**Why the stage log is a first-class return value.** Story S7.2 evaluates this
pipeline against a naive top-k baseline, which is only possible if each stage's
input and output can be inspected and each stage can be switched off. So
:class:`SearchResult` carries a :class:`StageLog` per stage — JSON-serialisable,
because the same records go into SSE events and RAGAS runs — and the disabling
knobs (``rewrites=0``, ``extract_filters=False``, ``use_rerank=False``) turn the
full pipeline into exactly that baseline without a second code path to keep in
step.

**Fusion happens over every (index, query) pair.** Three rewrites plus the
original, across two indexes, is eight rankings — that is RAG-Fusion, not eight
searches merged by hand. Rankings are named ``bm25#0``, ``dense#0``, ``bm25#1``
… so the fused hit records which index *and* which rewrite found it; the
per-result :attr:`RequirementResult.found_by` then collapses that back to the
two indexes, which is the level a user or a citation cares about.

**Hydration goes to SQLite, not to the index metadata.** A fused hit is only a
chunk id. The record behind it is fetched from SQLite — the source of truth —
so a result is a real ``Requirement`` or ``CodeUnit`` regardless of which index
found it and regardless of what that index happened to store. An id with no row
behind it (a stale index from an earlier build) is skipped and counted, never
fatal.

**Nothing here throws because a model failed.** Each LLM stage degrades on its
own terms — expansion to the original query, filtering to no filter, reranking
to fusion order — so a search survives all three being down. That is asserted
end to end in the tests, because it is the property that decides whether the
app is usable on a bad day.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import chromadb

from core import db
from core.embeddings import EmbeddingClient, EmbeddingUsage
from core.llm import Llm, LlmUsage
from core.manifest import ProjectManifest
from core.models import CodeUnit, Requirement
from retrieval import bm25, dense, hybrid, self_query, translate
from retrieval import rerank as rerank_stage
from retrieval.chunks import CODE_DOC_TYPE, ChunkRef, parse_chunk_key
from retrieval.lookup import Citation
from retrieval.self_query import Filter, QueryFilter

#: Results handed back to the agent. Spec §4 says five.
DEFAULT_TOP_N = 5

#: Candidates each (index, query) pair contributes. Eight rankings of ten
#: leaves fusion plenty to work with while keeping each Chroma query cheap.
DEFAULT_PER_QUERY_LIMIT = 10

#: Fused candidates carried into hydration and reranking. Spec §4 says twenty.
DEFAULT_FUSE_LIMIT = 20

#: ``search_requirements`` must never return code, whatever the extracted
#: filter says. Expressed as a metadata condition so it constrains the ANN
#: search rather than discarding results after it.
_NOT_CODE = {"doc_type": {"$ne": CODE_DOC_TYPE}}
_ONLY_CODE = {"doc_type": CODE_DOC_TYPE}


@dataclass(frozen=True)
class Engine:
    """Everything the pipeline needs, assembled once at API startup.

    A plain bag of injected dependencies: the stages stay pure functions of
    their arguments, and a test can substitute any one of them. The BM25 index
    is a value rather than something rebuilt per call — story S1.5.2 builds it
    at boot.

    .. warning::
       ``conn`` makes this **not** safe to share across threads, and WP3 has to
       decide what to do about that. ``core.db.connect`` uses sqlite3's default
       ``check_same_thread=True``, and FastAPI runs ``def`` (non-async)
       endpoints in a threadpool — so a single Engine parked in ``app.state``
       raises ``ProgrammingError: SQLite objects created in a thread can only
       be used in that same thread`` on the first request that lands on another
       worker. Measured, not theorised.

       The other three dependencies are fine to share: the BM25 index is
       immutable, the Chroma client is thread-safe, and the embedding client
       holds no connection of its own (it takes ``conn`` per call).

       So build the Engine per request from a per-request connection, keeping
       the index, collection and models from application state. Passing
       ``check_same_thread=False`` instead would silence the error without
       making interleaved use of one connection safe.
    """

    manifest: ProjectManifest
    conn: sqlite3.Connection
    collection: chromadb.Collection
    bm25_index: bm25.Bm25Index
    embeddings: EmbeddingClient
    translate_llm: Llm
    rerank_llm: Llm

    @property
    def project_id(self) -> str:
        return self.manifest.project_id


@dataclass(frozen=True)
class StageLog:
    """One stage's cost, duration and a JSON-serialisable summary of its work."""

    name: str
    elapsed_ms: float
    cost_usd: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "cost_usd": self.cost_usd,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PipelineUsage:
    """What one search cost, kept apart by purpose.

    :class:`~core.llm.LlmUsage` refuses to add two purposes together — a
    reranker's spend folded into the expander's line would misreport both — so
    the LLM side is a tuple of per-purpose records and the embedding side is
    separate again.
    """

    llm: tuple[LlmUsage, ...] = ()
    embedding: EmbeddingUsage = EmbeddingUsage()

    @property
    def cost_usd(self) -> float:
        return sum(usage.cost_usd for usage in self.llm) + self.embedding.cost_usd


@dataclass(frozen=True)
class RequirementResult:
    """One retrieved requirement, ready to cite."""

    requirement: Requirement
    citation: Citation
    rank: int
    fused_score: float
    #: Best rank per index, collapsed across rewrites (``{"bm25": 2, "dense": 1}``).
    found_by: dict[str, int]


@dataclass(frozen=True)
class CodeResult:
    """One retrieved code unit, ready to open in the code pane."""

    unit: CodeUnit
    rank: int
    fused_score: float
    found_by: dict[str, int]


@dataclass(frozen=True)
class SearchResult[T]:
    """Results, per-stage instrumentation, and the total cost."""

    query: str
    results: list[T]
    stages: list[StageLog]
    usage: PipelineUsage


class _Recorder:
    """Accumulates stage logs and usage while a search runs.

    Local to one search, so the pipeline functions stay reentrant — two
    concurrent requests share the Engine but never a recorder.
    """

    def __init__(self) -> None:
        self.stages: list[StageLog] = []
        self._llm: dict[str, LlmUsage] = {}
        self._embedding = EmbeddingUsage()

    def stage(
        self,
        name: str,
        started: float,
        *,
        cost_usd: float = 0.0,
        **detail: Any,
    ) -> None:
        self.stages.append(
            StageLog(
                name=name,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                cost_usd=cost_usd,
                detail=detail,
            )
        )

    def llm(self, usage: LlmUsage) -> float:
        """Record ``usage`` under its purpose; return its cost."""
        existing = self._llm.get(usage.purpose)
        self._llm[usage.purpose] = existing.plus(usage) if existing else usage
        return usage.cost_usd

    def embedding(self, usage: EmbeddingUsage) -> float:
        """Accumulate a query embedding's usage; return its cost.

        Added field by field rather than through ``EmbeddingUsage.plus``, which
        takes a provider *batch* — this side is summing already-summarised
        usage from several dense searches within one pipeline run.
        """
        self._embedding = EmbeddingUsage(
            purpose=self._embedding.purpose,
            calls=self._embedding.calls + usage.calls,
            retries=self._embedding.retries + usage.retries,
            prompt_tokens=self._embedding.prompt_tokens + usage.prompt_tokens,
            total_tokens=self._embedding.total_tokens + usage.total_tokens,
            cost_usd=self._embedding.cost_usd + usage.cost_usd,
        )
        return usage.cost_usd

    def usage(self) -> PipelineUsage:
        return PipelineUsage(llm=tuple(self._llm.values()), embedding=self._embedding)


# --------------------------------------------------------------------------
# search_requirements
# --------------------------------------------------------------------------


def search_requirements(
    engine: Engine,
    query: str,
    *,
    filters: QueryFilter | None = None,
    top_n: int = DEFAULT_TOP_N,
    rewrites: int = translate.DEFAULT_REWRITE_COUNT,
    extract_filters: bool = True,
    use_rerank: bool = True,
    per_query_limit: int = DEFAULT_PER_QUERY_LIMIT,
    fuse_limit: int = DEFAULT_FUSE_LIMIT,
) -> SearchResult[RequirementResult]:
    """The advanced-RAG pipeline as one call — tool ``search_requirements``.

    ``filters`` skips the self-query stage: the tool signature lets a caller
    state a constraint outright rather than have one inferred. Setting
    ``rewrites=0``, ``extract_filters=False`` and ``use_rerank=False`` reduces
    this to the naive single-query top-k baseline story S7.2 compares against.

    Code chunks are never returned, whatever the filter says.
    """
    cleaned = _require_query(query)
    recorder = _Recorder()

    queries = [cleaned]
    if rewrites > 0:
        started = time.perf_counter()
        expansion = translate.expand(
            engine.translate_llm, cleaned, manifest=engine.manifest, count=rewrites
        )
        queries = expansion.queries
        recorder.stage(
            "expand",
            started,
            cost_usd=recorder.llm(expansion.usage),
            queries=list(expansion.queries),
            dropped=len(expansion.dropped),
            fallback_reason=expansion.fallback_reason,
        )

    resolved = _resolve_filters(
        engine,
        cleaned,
        filters,
        extract_filters,
        recorder,
        restrict=_requirements_only_doc_type,
    )

    hydrated, fused_by_id = _retrieve_fuse_hydrate(
        engine,
        queries,
        recorder,
        conditions=[*resolved.conditions(), _NOT_CODE],
        bm25_predicate=lambda record: resolved.matches(record)
        and record.doc_type != CODE_DOC_TYPE,
        per_query_limit=per_query_limit,
        fuse_limit=fuse_limit,
        want="requirement",
    )

    ordered = _maybe_rerank(
        engine,
        cleaned,
        hydrated,
        recorder,
        use_rerank=use_rerank,
        top_n=top_n,
        label=_requirement_label,
        text=lambda requirement: requirement.text,
    )

    results = [
        RequirementResult(
            requirement=requirement,
            citation=_citation(requirement),
            rank=rank,
            fused_score=fused_by_id[chunk_id].score,
            found_by=_collapse(fused_by_id[chunk_id].ranks),
        )
        for rank, (chunk_id, requirement) in enumerate(ordered, start=1)
    ]
    return SearchResult(
        query=cleaned, results=results, stages=recorder.stages, usage=recorder.usage()
    )


# --------------------------------------------------------------------------
# search_code
# --------------------------------------------------------------------------


def search_code(
    engine: Engine,
    query: str | None = None,
    *,
    symbol: str | None = None,
    top_n: int = DEFAULT_TOP_N,
    use_rerank: bool = True,
    per_query_limit: int = DEFAULT_PER_QUERY_LIMIT,
    fuse_limit: int = DEFAULT_FUSE_LIMIT,
) -> SearchResult[CodeResult]:
    """AST-chunk retrieval over the pinned snapshot — tool ``search_code``.

    Shorter than :func:`search_requirements` by design, in two ways.

    **An exact symbol short-circuits everything.** ``search_code(symbol="X")``
    is a lookup, not a search: SQLite has an index on ``symbol``, the answer is
    exact, and it costs nothing. Definitions are returned before declarations,
    because a header prototype is not an implementation
    (``ingestion/code_indexer.py``) and the evidence engine (WP4) must see the
    stronger evidence first.

    **There is no query expansion or filter extraction.** Code queries are
    symbol-shaped — ``CanIf_Transmit``, ``bus off handler`` — which is what BM25
    is already best at; rewriting them buys cost rather than recall, and there
    is no section metadata on code to self-query against.
    """
    if query is None and symbol is None:
        raise ValueError("search_code needs a query or a symbol")
    recorded_query = (symbol or query or "").strip()
    if not recorded_query:
        raise ValueError("cannot search for an empty query or symbol")

    recorder = _Recorder()

    started = time.perf_counter()
    exact: list[CodeUnit] = []
    if symbol:
        exact = _by_symbol(engine, symbol)
    recorder.stage("symbol", started, symbol=symbol, exact_matches=len(exact))

    if exact:
        results = [
            CodeResult(unit=unit, rank=rank, fused_score=0.0, found_by={"symbol": rank})
            for rank, unit in enumerate(exact[:top_n], start=1)
        ]
        return SearchResult(
            query=recorded_query, results=results, stages=recorder.stages, usage=recorder.usage()
        )

    hydrated, fused_by_id = _retrieve_fuse_hydrate(
        engine,
        [recorded_query],
        recorder,
        conditions=[_ONLY_CODE],
        bm25_predicate=lambda record: record.doc_type == CODE_DOC_TYPE,
        per_query_limit=per_query_limit,
        fuse_limit=fuse_limit,
        want="code",
    )

    ordered = _maybe_rerank(
        engine,
        recorded_query,
        hydrated,
        recorder,
        use_rerank=use_rerank,
        top_n=top_n,
        label=_code_label,
        text=lambda unit: unit.text,
    )

    results = [
        CodeResult(
            unit=unit,
            rank=rank,
            fused_score=fused_by_id[chunk_id].score,
            found_by=_collapse(fused_by_id[chunk_id].ranks),
        )
        for rank, (chunk_id, unit) in enumerate(ordered, start=1)
    ]
    return SearchResult(
        query=recorded_query, results=results, stages=recorder.stages, usage=recorder.usage()
    )


# --------------------------------------------------------------------------
# the shared middle: retrieve, fuse, hydrate
# --------------------------------------------------------------------------


def _retrieve_fuse_hydrate(
    engine: Engine,
    queries: Sequence[str],
    recorder: _Recorder,
    *,
    conditions: list[dict[str, Any]],
    bm25_predicate,
    per_query_limit: int,
    fuse_limit: int,
    want: str,
) -> tuple[list[tuple[str, Any]], dict[str, hybrid.FusedHit]]:
    """Run both indexes over every query, fuse, and load the real records."""
    where = self_query.combine(conditions)

    started = time.perf_counter()
    rankings: dict[str, list[str]] = {}
    embedding_cost = 0.0
    for position, one in enumerate(queries):
        lexical = bm25.search(
            engine.bm25_index, one, limit=per_query_limit, predicate=bm25_predicate
        )
        rankings[f"{hybrid.BM25_SOURCE}#{position}"] = [hit.record.id for hit in lexical]

        vectors = dense.search(
            engine.collection,
            engine.embeddings,
            one,
            conn=engine.conn,
            limit=per_query_limit,
            where=where,
        )
        embedding_cost += recorder.embedding(vectors.usage)
        rankings[f"{hybrid.DENSE_SOURCE}#{position}"] = [hit.id for hit in vectors.hits]

    recorder.stage(
        "retrieve",
        started,
        cost_usd=embedding_cost,
        queries=len(queries),
        where=where,
        hits={name: len(ids) for name, ids in rankings.items()},
    )

    started = time.perf_counter()
    fused = hybrid.reciprocal_rank_fusion(rankings, limit=fuse_limit)
    recorder.stage("fuse", started, candidates=len(fused), k=hybrid.DEFAULT_RRF_K)

    started = time.perf_counter()
    hydrated: list[tuple[str, Any]] = []
    unresolved = 0
    for hit in fused:
        reference = parse_chunk_key(hit.id)
        if reference is None or reference.kind != want:
            unresolved += 1
            continue
        record = _load(engine, reference)
        if record is None:
            unresolved += 1
            continue
        hydrated.append((hit.id, record))
    recorder.stage("hydrate", started, resolved=len(hydrated), unresolved=unresolved)

    return hydrated, {hit.id: hit for hit in fused}


def _load(engine: Engine, reference: ChunkRef) -> Requirement | CodeUnit | None:
    """Fetch the record a chunk id names from SQLite, the source of truth."""
    if reference.kind == "requirement" and reference.req_id is not None:
        return db.get_requirement(engine.conn, engine.project_id, reference.req_id)
    if reference.kind == "code" and reference.repo_path is not None:
        return db.get_code_unit(
            engine.conn,
            engine.project_id,
            reference.repo_path,
            reference.code_kind or "",
            reference.symbol or "",
            reference.line_span or (0, 0),
        )
    return None


def _maybe_rerank(
    engine: Engine,
    query: str,
    hydrated: list[tuple[str, Any]],
    recorder: _Recorder,
    *,
    use_rerank: bool,
    top_n: int,
    label,
    text,
) -> list[tuple[str, Any]]:
    """Rerank if asked, else keep fusion order. Either way truncate to ``top_n``."""
    if not use_rerank or not hydrated:
        return hydrated[:top_n]

    started = time.perf_counter()
    candidates = [
        rerank_stage.Candidate(id=chunk_id, text=text(record), label=label(record))
        for chunk_id, record in hydrated
    ]

    reranked = rerank_stage.rerank(engine.rerank_llm, query, candidates, top_n=top_n)
    recorder.stage(
        "rerank",
        started,
        cost_usd=recorder.llm(reranked.usage),
        candidates=len(candidates),
        returned=len(reranked.order),
        completed_from_fusion=reranked.completed_from_fusion,
        fallback_reason=reranked.fallback_reason,
    )

    by_id = dict(hydrated)
    return [(chunk_id, by_id[chunk_id]) for chunk_id in reranked.order if chunk_id in by_id]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _require_query(query: str) -> str:
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("cannot search for an empty query")
    return cleaned


def _resolve_filters(
    engine: Engine,
    query: str,
    filters: QueryFilter | None,
    extract_filters: bool,
    recorder: _Recorder,
    *,
    restrict: Callable[[Filter], Filter] = lambda resolved: resolved,
) -> Filter:
    """The filter to apply: the caller's, the model's, or none.

    ``restrict`` is the calling search's own narrowing rule, applied *before*
    the stage is logged so the log shows the filter that was actually used
    rather than the one that was proposed.
    """
    if filters is not None:
        started = time.perf_counter()
        resolved = restrict(
            self_query.resolve(
                filters, manifest=engine.manifest, conn=engine.conn, project_id=engine.project_id
            )
        )
        recorder.stage(
            "self_query",
            started,
            source="caller",
            module=resolved.module,
            doc_type=resolved.doc_type,
            sections=list(resolved.section_paths),
            dropped=dict(resolved.dropped),
            fallback_reason=None,
        )
        return resolved

    if not extract_filters:
        return Filter()

    started = time.perf_counter()
    extraction = self_query.extract(
        engine.translate_llm,
        query,
        manifest=engine.manifest,
        conn=engine.conn,
        project_id=engine.project_id,
    )
    resolved = restrict(extraction.filter)
    recorder.stage(
        "self_query",
        started,
        cost_usd=recorder.llm(extraction.usage),
        source="model",
        module=resolved.module,
        doc_type=resolved.doc_type,
        sections=list(resolved.section_paths),
        dropped=dict(resolved.dropped),
        fallback_reason=extraction.fallback_reason,
    )
    return resolved


def _requirements_only_doc_type(resolved: Filter) -> Filter:
    """In a requirement search, only ``doc_type=requirement`` is honourable.

    A ``doc_type`` filter here may narrow *to* requirements but must never
    narrow *away* from them, so ``code`` and ``context`` are both dropped.

    ``code`` is the obvious case: it would ``$and`` ``doc_type == code`` with
    ``doc_type != code`` and match nothing.

    ``context`` is the case measured on the real corpus, and it is the reason
    this rule is not just about code. "How does the driver report bus-off?" is
    a how-does-it-work question, so the model extracted ``doc_type=context`` —
    and the search came back with five prose chunks and not one requirement.
    The same filter reduced "which scheduled functions does the CAN driver
    have?" to a single context chunk, excluding SWS_Can_00110, the requirement
    that answers it.

    The asymmetry settles it. Applying the filter hides the normative content
    the whole tool exists to find; dropping it only widens the search, and the
    prose chunks still rank perfectly well for a definitional question — they
    did before any filter existed. Same rule as
    :mod:`retrieval.self_query`: drop the field, record why, keep searching.
    """
    if resolved.doc_type in (None, "requirement"):
        return resolved
    reason = (
        "a requirement search cannot return code chunks"
        if resolved.doc_type == CODE_DOC_TYPE
        else "filtering a requirement search to prose would exclude every requirement"
    )
    return replace(
        resolved,
        doc_type=None,
        dropped=(*resolved.dropped, ("doc_type", reason)),
    )


def _collapse(ranks: dict[str, int]) -> dict[str, int]:
    """``{"bm25#0": 3, "bm25#2": 1}`` → ``{"bm25": 1}``.

    Fusion names a ranking per (index, rewrite) pair, which is what makes
    RAG-Fusion work; a citation only cares which *index* found the chunk and
    how well, so the best rank per index is what surfaces.
    """
    best: dict[str, int] = {}
    for name, rank in ranks.items():
        source = name.split("#", 1)[0]
        if source not in best or rank < best[source]:
            best[source] = rank
    return best


def _citation(requirement: Requirement) -> Citation:
    return Citation(
        req_id=requirement.id,
        doc=requirement.source_doc,
        page=requirement.page,
        bbox=requirement.bbox,
    )


def _requirement_label(requirement: Requirement) -> str:
    parts = [requirement.id]
    if requirement.section_path:
        parts.append(requirement.section_path)
    if requirement.title:
        parts.append(requirement.title)
    return " — ".join(parts)


def _code_label(unit: CodeUnit) -> str:
    start, end = unit.line_span
    return f"{unit.repo_path}:{start}-{end} ({unit.kind} {unit.symbol})"


#: Definitions before declarations: a prototype is not an implementation, so
#: the evidence engine (WP4) must be shown the stronger candidate first.
_KIND_PRIORITY = {"function": 0}


def _by_symbol(engine: Engine, symbol: str) -> list[CodeUnit]:
    units = db.list_code_units_by_symbol(engine.conn, engine.project_id, symbol.strip())
    return sorted(
        units,
        key=lambda unit: (
            _KIND_PRIORITY.get(unit.kind, 1),
            unit.repo_path,
            unit.line_span[0],
        ),
    )
