"""The ingestion CLI (story S1.5.3) — two URLs and a git SHA to a queryable index.

    cd backend
    uv run python -m ingestion.run ../projects/autosar-can/project.yaml

This is the **only** entry point for ingestion. It composes what D2–D5 built
rather than reimplementing any of it, in this order:

1. load and validate the manifest (``core.manifest``)
2. fetch the corpus PDFs, skip-if-present (``ingestion.fetcher``)
3. parse, extract requirements and chunk context prose; write
   ``data/extraction_report.md`` and ``data/spot_check.md``
   (``ingestion.pdf_loader``, ``req_extractor``, ``context_chunker``,
   ``extraction_report``, ``spot_check``)
4. fetch the pinned repository snapshot, skip-if-present, then chunk it and
   scan annotations (``ingestion.code_fetcher``, ``code_indexer``,
   ``annotations``)
5. persist everything to SQLite (``core.db``)
6. embed every chunk, cache-aware, and upsert into Chroma
   (``core.embeddings``, ``retrieval.vector_store``)

then builds the BM25 index from SQLite and runs a smoke query, so a run ends
by proving the index it just wrote can be searched.

**Idempotent end to end.** A second run over an intact ``data/`` downloads
nothing, embeds nothing and leaves the same row counts — that is the
acceptance criterion for three of the stories this composes, so it is a
property of the pipeline rather than a nicety.

**Ruling R26: report first, fail second.** Duplicate requirement ids are a
*warning* in the extractor (which cannot see the storage constraint) and fatal
*here*. Raising in the extractor would suppress the very
``extraction_report.md`` that names the collision, so instead this CLI writes
the report and *then* exits non-zero on any extractor warning. That ordering
is the whole point: the evidence survives the failure.

The ruling's original wording said the error lands "at the SQLite insert". It
cannot, and the ruling is amended rather than pretended: ``core/db.py``'s
upserts are ``ON CONFLICT DO UPDATE``, which idempotent re-ingestion
*requires* — a second run must overwrite its own rows, not fail on them. So
the gate is entirely in this module, and it is two checks, not one: the
extractor's per-document warning (an id twice in one document), plus
:func:`cross_document_duplicates` (the same id in two documents), which no
per-document pass can see and which the upsert would otherwise silently
merge. Today's corpus has neither — 1054 ids, 1054 unique.

**Never a stack trace.** Every foreseeable fault — a missing key, an
unreachable URL, a SHA mismatch, a manifest error, an unwritable path — is
caught and printed as one readable line, per the spec's first-run promise.
Progress is reported through the ``on_progress`` hooks D3 and D5 exposed
rather than by ``print`` calls inside the engines, so story S3.6.2 can stream
the same pipeline over SSE without touching them.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from core import db, paths
from core.embeddings import (
    DEFAULT_BATCH_SIZE,
    EmbeddingError,
    EmbeddingUsage,
    Transport,
    api_key,
    client_for,
)
from core.manifest import ManifestError, ProjectManifest, load_manifest
from core.models import CodeUnit, Requirement
from ingestion.annotations import AnnotationError
from ingestion.code_fetcher import (
    CodeFetchError,
    GitRunner,
    fetch_repo,
    repo_dir,
    subprocess_git_runner,
)
from ingestion.code_indexer import (
    FUNCTION_KIND,
    PROTOTYPE_KIND,
    RepoIndexResult,
    index_repo,
)
from ingestion.extraction_report import (
    DocumentIngestion,
    ExtractionReportError,
    ingest_document,
    write_extraction_report,
)
from ingestion.fetcher import (
    Downloader,
    FetchError,
    FetchResult,
    docs_dir,
    fetch_documents,
    urllib_downloader,
)
from ingestion.spot_check import SpotCheckPick, write_spot_check
from retrieval import bm25, vector_store
from retrieval.chunks import (
    IndexDocument,
    code_document,
    module_for_path,
    module_names,
    requirement_document,
)

#: Pipeline stages, in order. ``--stop-after`` names one of these.
STAGES = ("docs", "extract", "code", "store", "embed")


class IngestionError(Exception):
    """A pipeline fault worth reporting as one line rather than a traceback."""


# --------------------------------------------------------------------------
# options and progress
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Options:
    """Everything the run's behaviour depends on, in one value."""

    force: bool = False
    skip_embeddings: bool = False
    stop_after: str = STAGES[-1]
    db_path: Path | None = None
    chroma_path: Path | None = None
    batch_size: int = DEFAULT_BATCH_SIZE

    def runs(self, stage: str) -> bool:
        """True when ``stage`` is at or before :attr:`stop_after`."""
        return STAGES.index(stage) <= STAGES.index(self.stop_after)


class Reporter:
    """Line-based progress output. Silent when ``quiet``.

    Deliberately dumb: the engines take ``on_progress`` callbacks and this is
    one implementation of them. Story S3.6.2 substitutes an SSE one.
    """

    def __init__(self, stream: TextIO | None = None, *, quiet: bool = False) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.quiet = quiet
        self._seen: set[str] = set()

    def line(self, text: str = "") -> None:
        if not self.quiet:
            print(text, file=self.stream, flush=True)

    def stage(self, name: str, detail: str) -> None:
        number = STAGES.index(name) + 1
        self.line(f"[{number}/{len(STAGES)}] {name}: {detail}")

    def once(self, key: str, text: str) -> None:
        """Print ``text`` the first time ``key`` is seen (per-file download lines)."""
        if key not in self._seen:
            self._seen.add(key)
            self.line(text)

    def ticker(self, label: str, *, steps: int = 10):
        """A ``(item, done, total)`` callback that prints at most ``steps`` lines."""
        state = {"next": 0}

        def on_progress(item: str, done: int, total: int | None) -> None:
            if self.quiet or total is None or total <= 0:
                return
            stride = max(total // steps, 1)
            if done >= state["next"] or done == total:
                state["next"] = done + stride
                percent = 100 * done // total
                self.line(f"      {label} {done}/{total} ({percent}%) {item}")

        return on_progress


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentSummary:
    key: str
    filename: str
    module: str
    fetch_action: str
    requirements: int
    expected: int
    context_chunks: int
    misses: int
    warnings: int


@dataclass(frozen=True)
class CodeSummary:
    fetch_action: str
    git_sha: str
    files: int
    units: int
    kinds: dict[str, int]
    annotations: int
    claimed: int
    not_claimed: int
    distinct_ids: int
    partial_files: int


@dataclass(frozen=True)
class JoinSummary:
    """Tier-1 join: how many annotated ids exist as ingested requirements.

    Reported twice on purpose. The corpus-wide rate is depressed by modules
    whose specifications are deliberately not ingested (CanNm, Com, PduR have
    annotations in the code but no SWS document here), so the rate restricted
    to *ingested* modules is the one that says whether canonicalization works.
    """

    distinct_ids: int
    matched: int
    per_module: dict[str, tuple[int, int]]
    ingested_modules: tuple[str, ...]

    @property
    def rate(self) -> float:
        return self.matched / self.distinct_ids if self.distinct_ids else 0.0

    @property
    def ingested_totals(self) -> tuple[int, int]:
        known = {m.upper() for m in self.ingested_modules}
        matched = total = 0
        for module, (hit, count) in self.per_module.items():
            if module.upper() in known:
                matched += hit
                total += count
        return matched, total

    @property
    def ingested_rate(self) -> float:
        matched, total = self.ingested_totals
        return matched / total if total else 0.0


@dataclass(frozen=True)
class EmbedSummary:
    skipped: bool = False
    reason: str | None = None
    chunks: int = 0
    computed: int = 0
    cached: int = 0
    duplicates: int = 0
    upserted: int = 0
    collection_count: int = 0
    prototypes_dropped: int = 0
    usage: EmbeddingUsage = EmbeddingUsage()


@dataclass(frozen=True)
class IndexCheck:
    """BM25 build from SQLite plus one smoke query — story S1.5.2's criterion."""

    records: int
    elapsed_seconds: float
    query: str | None = None
    top_id: str | None = None
    top_score: float = 0.0
    hits: int = 0


@dataclass(frozen=True)
class RunSummary:
    project_id: str
    documents: list[DocumentSummary] = field(default_factory=list)
    code: CodeSummary | None = None
    join: JoinSummary | None = None
    rows: dict[str, int] = field(default_factory=dict)
    embeddings: EmbedSummary = EmbedSummary()
    index_check: IndexCheck | None = None
    artifacts: list[Path] = field(default_factory=list)
    spot_checks: list[SpotCheckPick] = field(default_factory=list)
    elapsed_seconds: float = 0.0


def _rate(count: int, total: int) -> str:
    return f"{100.0 * count / total:.1f}%" if total else "n/a"


def render_summary(summary: RunSummary) -> str:
    """The final report a run prints. One place, so two runs are comparable."""
    lines = [
        "",
        "=" * 78,
        f"ingestion summary — project {summary.project_id!r} "
        f"({summary.elapsed_seconds:.1f}s)",
        "=" * 78,
        "",
        f"{'document':<18}{'fetch':<14}{'reqs':>6}{'exp':>6}{'ctx':>6}{'miss':>6}",
    ]
    for document in summary.documents:
        lines.append(
            f"{document.key:<18}{document.fetch_action:<14}{document.requirements:>6}"
            f"{document.expected:>6}{document.context_chunks:>6}{document.misses:>6}"
        )
    if summary.documents:
        lines.append(
            f"{'total':<18}{'':<14}"
            f"{sum(d.requirements for d in summary.documents):>6}"
            f"{sum(d.expected for d in summary.documents):>6}"
            f"{sum(d.context_chunks for d in summary.documents):>6}"
            f"{sum(d.misses for d in summary.documents):>6}"
        )

    if summary.code is not None:
        code = summary.code
        kinds = ", ".join(f"{kind} {count}" for kind, count in sorted(code.kinds.items()))
        lines.extend(
            [
                "",
                f"code           {code.fetch_action} at {code.git_sha[:12]}",
                f"  files        {code.files} in scope"
                + (f" ({code.partial_files} partially parsed)" if code.partial_files else ""),
                f"  code units   {code.units} ({kinds})",
                f"  annotations  {code.annotations} "
                f"(@req {code.claimed} + !req {code.not_claimed}), "
                f"{code.distinct_ids} distinct canonical ids",
            ]
        )

    if summary.join is not None:
        join = summary.join
        matched, total = join.ingested_totals
        lines.extend(
            [
                "",
                f"tier-1 join    {join.matched}/{join.distinct_ids} distinct annotated ids "
                f"exist as requirements ({_rate(join.matched, join.distinct_ids)})",
                f"  ingested     {matched}/{total} "
                f"({_rate(matched, total)}) for modules whose specifications are ingested "
                f"({', '.join(join.ingested_modules)})",
            ]
        )
        for module, (hit, count) in sorted(join.per_module.items()):
            marker = "*" if module.upper() in {m.upper() for m in join.ingested_modules} else " "
            lines.append(f"  {marker} {module:<12}{hit:>5}/{count:<5} {_rate(hit, count)}")
        lines.append("  (* specification ingested; the rest are release drift by design)")

    if summary.rows:
        lines.append("")
        lines.append("sqlite rows")
        for table, count in summary.rows.items():
            lines.append(f"  {table:<24}{count}")

    embed = summary.embeddings
    lines.append("")
    if embed.skipped:
        lines.append(f"embeddings     skipped — {embed.reason}")
    else:
        lines.extend(
            [
                f"embeddings     {embed.chunks} chunk(s): {embed.computed} computed, "
                f"{embed.cached} from cache, {embed.duplicates} in-request duplicate(s)",
                f"  chroma       {embed.upserted} upserted, collection holds "
                f"{embed.collection_count}",
                f"  api calls    {embed.usage.calls} "
                f"({embed.usage.retries} retry/retries), "
                f"{embed.usage.total_tokens} tokens",
                f"  cost         ${embed.usage.cost_usd:.6f} "
                f"(reported by OpenRouter, not estimated)",
            ]
        )
        if embed.prototypes_dropped:
            lines.append(
                f"  not embedded {embed.prototypes_dropped} unannotated prototype(s) whose "
                "definition is already indexed (still in SQLite and BM25)"
            )

    if summary.index_check is not None:
        check = summary.index_check
        lines.extend(
            [
                "",
                f"bm25 startup   {check.records} record(s) loaded from SQLite and indexed "
                f"in {check.elapsed_seconds:.2f}s (criterion: < 5s)",
            ]
        )
        if check.query is not None:
            lines.append(
                f"  smoke query  {check.query!r} -> {check.hits} hit(s), "
                f"top {check.top_id} (score {check.top_score:.2f})"
            )

    if summary.artifacts:
        lines.append("")
        lines.append("artifacts")
        for path in summary.artifacts:
            lines.append(f"  {path}")
    if summary.spot_checks:
        lines.append(
            f"  spot check covers {len(summary.spot_checks)} requirement(s): "
            + ", ".join(pick.requirement.id for pick in summary.spot_checks)
        )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    """Open the registry, creating the schema if needed.

    ``synchronous = NORMAL`` because ``core.db`` commits per row for the
    registry tables and this database is entirely derived: the worst a crash
    mid-ingestion can cost is a re-run of a command that is idempotent by
    design. With ``FULL`` every one of those commits fsyncs, for a durability
    guarantee nobody here can use.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(path)
    conn.execute("PRAGMA synchronous = NORMAL")
    db.migrate(conn)
    return conn


def cross_document_duplicates(results: Sequence[DocumentIngestion]) -> list[str]:
    """Ids that two *different* documents both produced, as report lines.

    The other half of ruling R26. The extractor warns about an id appearing
    twice in one document, but it sees one document at a time, and
    ``requirements``' primary key is ``(project_id, id)`` across the whole
    corpus — so a cross-document collision would be silently merged by the
    upsert, leaving one requirement wearing the other's text. Nothing else in
    the pipeline can catch it.

    Matched on :meth:`Requirement.canonical_id`, not the raw id: lookup is
    case-insensitive (``core/db.py``), so ``SWS_Can_00491`` and
    ``SWS_CAN_00491`` in two documents would be one requirement to every
    reader even though SQLite would store both rows.

    Context chunks are checked alongside requirements because they share the
    table and the key.
    """
    owner: dict[str, tuple[str, str]] = {}
    collisions: list[str] = []
    for result in results:
        key = result.entry.key
        for record in (*result.extraction.requirements, *result.context_chunks):
            canonical = Requirement.canonical_id(record.id)
            previous = owner.get(canonical)
            if previous is None:
                owner[canonical] = (key, record.id)
            elif previous[0] != key:
                collisions.append(
                    f"{key}: id {record.id} was already extracted from {previous[0]} "
                    f"as {previous[1]} — two documents cannot share one requirement id"
                )
    return collisions


def embeddable_code_units(units: Sequence[CodeUnit]) -> tuple[list[CodeUnit], int]:
    """Split code units into "worth a vector" and a count of what was dropped.

    **An unannotated prototype whose symbol also has a definition in the index
    is not embedded.** Everything else is.

    The rationale, measured on this corpus: of 301 ``prototype`` units, 226
    carry no annotation, and 217 of those 226 (96%) have a ``function``
    definition of the same symbol in the same index. 197 of the 226 are a
    single line and only 24 contain any comment at all. So the typical dropped
    unit is a bare ``Std_ReturnType Can_Write(...);`` whose vector sits a
    hair's breadth from the definition's — it costs money to compute, and it
    costs *retrieval quality*: it consumes a slot in every top-k and hands the
    reranker two near-identical candidates instead of two different ideas.

    What is deliberately kept:

    * **every annotated prototype.** An annotation on a declaration is tier-1
      evidence (weak evidence, but evidence) and 188 distinct annotated ids
      have their only claimed-implemented evidence on a prototype or a
      file-scope unit. Dropping those would make ``check_implementation``
      answer "no evidence" where evidence exists.
    * **the 9 unannotated prototypes with no definition in scope** — the
      declaration is the only place that API appears here, so it is not a
      duplicate of anything.

    This is a *vector-store* decision only. All 1253 units stay in SQLite, so
    BM25 and exact-symbol lookup (``list_code_units_by_symbol``, the tier-2
    anchor) are untouched; only dense paraphrase retrieval loses the
    duplicate. The rule is a deterministic function of the snapshot, so two
    runs drop exactly the same units.
    """
    defined = {unit.symbol for unit in units if unit.kind == FUNCTION_KIND}
    kept: list[CodeUnit] = []
    dropped = 0
    for unit in units:
        if unit.kind == PROTOTYPE_KIND and not unit.req_annotations and unit.symbol in defined:
            dropped += 1
            continue
        kept.append(unit)
    return kept, dropped


def _annotation_modules(manifest: ProjectManifest, index: RepoIndexResult) -> dict[str, str]:
    """``canonical_id -> module``, recovered from each annotation's raw text.

    The module is re-parsed with the manifest's own ``annotation_pattern`` and
    mapped through its ``annotation_module_map``, rather than being guessed
    from the shape of the canonical id — the id template is a manifest value
    and this module must not assume its layout.
    """
    pattern = re.compile(manifest.code.annotation_pattern)
    lookup = {key.upper(): value for key, value in manifest.code.annotation_module_map.items()}
    modules: dict[str, str] = {}
    for unit in index.units:
        for annotation in unit.req_annotations:
            if annotation.canonical_id in modules:
                continue
            match = pattern.search(annotation.raw)
            if match is None:
                continue
            raw_module = match.group("module")
            modules[annotation.canonical_id] = lookup.get(raw_module.upper(), raw_module)
    return modules


def tier1_join(
    conn: sqlite3.Connection,
    manifest: ProjectManifest,
    index: RepoIndexResult,
) -> JoinSummary:
    """How many distinct annotated ids exist as ingested requirements."""
    known = {
        row["canonical_id"]
        for row in conn.execute(
            "SELECT canonical_id FROM requirements WHERE project_id = ?",
            (manifest.project_id,),
        )
    }
    modules = _annotation_modules(manifest, index)
    per_module: dict[str, tuple[int, int]] = {}
    matched = 0
    for canonical_id in index.canonical_ids:
        hit = Requirement.canonical_id(canonical_id) in known
        matched += int(hit)
        module = modules.get(canonical_id, "unknown")
        previous_hits, previous_total = per_module.get(module, (0, 0))
        per_module[module] = (previous_hits + int(hit), previous_total + 1)
    return JoinSummary(
        distinct_ids=len(index.canonical_ids),
        matched=matched,
        per_module=per_module,
        ingested_modules=tuple(document.module for document in manifest.documents),
    )


def _index_documents(
    manifest: ProjectManifest,
    results: Sequence[DocumentIngestion],
    units: Sequence[CodeUnit],
) -> list[IndexDocument]:
    """Every chunk that gets a vector, requirements first then code."""
    documents: list[IndexDocument] = []
    for result in results:
        module = result.entry.module
        for requirement in result.extraction.requirements:
            documents.append(requirement_document(requirement, module=module))
        for chunk in result.context_chunks:
            documents.append(requirement_document(chunk, module=module))
    modules = module_names(manifest)
    for unit in units:
        documents.append(code_document(unit, module=module_for_path(unit.repo_path, modules)))
    return documents


def _row_counts(conn: sqlite3.Connection, manifest: ProjectManifest) -> dict[str, int]:
    project = (manifest.project_id,)
    counts: dict[str, int] = {}
    for label, sql in (
        ("requirements", "SELECT COUNT(*) AS n FROM requirements WHERE project_id = ? "
                         "AND doc_type = 'requirement'"),
        ("context chunks", "SELECT COUNT(*) AS n FROM requirements WHERE project_id = ? "
                           "AND doc_type = 'context'"),
        ("code units", "SELECT COUNT(*) AS n FROM code_units WHERE project_id = ?"),
    ):
        counts[label] = conn.execute(sql, project).fetchone()["n"]
    counts["embedding cache"] = conn.execute(
        "SELECT COUNT(*) AS n FROM embedding_cache"
    ).fetchone()["n"]
    return counts


def _smoke_query(
    index: bm25.Bm25Index, results: Sequence[DocumentIngestion]
) -> tuple[str | None, list[bm25.Bm25Hit]]:
    """One BM25 query, using a symbol the corpus itself named.

    Picking the term out of the extracted data rather than hard-coding one
    keeps this honest for a different corpus: the query is always a token the
    index is supposed to contain.
    """
    for result in results:
        for requirement in result.extraction.requirements:
            if requirement.named_symbols:
                query = requirement.named_symbols[0]
                return query, bm25.search(index, query, limit=3)
    return None, []


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------


def run_ingestion(
    manifest: ProjectManifest,
    options: Options | None = None,
    *,
    reporter: Reporter | None = None,
    downloader: Downloader | None = None,
    git_runner: GitRunner | None = None,
    transport: Transport | None = None,
) -> RunSummary:
    """Run the whole pipeline for ``manifest``. Raises :class:`IngestionError`.

    ``downloader``, ``git_runner`` and ``transport`` are injected so tests can
    drive the entire pipeline with no network and *prove* a second run makes
    no calls. They default to ``None`` and are resolved here rather than in the
    signature, so a default bound at import time cannot outlive a patch.
    """
    download = downloader if downloader is not None else urllib_downloader
    git = git_runner if git_runner is not None else subprocess_git_runner
    opts = options if options is not None else Options()
    report = reporter if reporter is not None else Reporter()
    started = time.perf_counter()

    report.line(f"project        {manifest.project_id} ({manifest.name}) {manifest.version}")
    report.line(f"embedding      {manifest.models.embedding}")
    report.line(f"documents      {len(manifest.documents)}")
    report.line("")

    # Preflight the key before anything slow: a fresh clone with no key should
    # learn that in a tenth of a second, not after three minutes of parsing.
    if opts.runs("embed") and not opts.skip_embeddings and transport is None:
        api_key()

    db_file = opts.db_path if opts.db_path is not None else paths.db_path(manifest)
    artifacts: list[Path] = []

    # -- 2. documents ------------------------------------------------------
    report.stage("docs", f"fetching {len(manifest.documents)} PDF(s)")
    docs_target = docs_dir(manifest)
    fetches: list[FetchResult] = fetch_documents(
        manifest,
        docs_target,
        downloader=download,
        on_progress=lambda filename, done, total: report.once(
            filename, f"      downloading {filename} ..."
        ),
        force=opts.force,
    )
    for fetched in fetches:
        report.line(f"      {fetched.action:<14}{fetched.filename} ({fetched.bytes:,} bytes)")
    if not opts.runs("extract"):
        return RunSummary(
            project_id=manifest.project_id,
            elapsed_seconds=time.perf_counter() - started,
        )

    # -- 3. requirements, context chunks, and the evidence artifacts -------
    report.stage("extract", "parsing PDFs and extracting requirements")
    results: list[DocumentIngestion] = []
    for entry, fetched in zip(manifest.documents, fetches, strict=True):
        result = ingest_document(manifest, entry, fetched.path)
        results.append(result)
        report.line(
            f"      {entry.key:<18}{len(result.extraction.requirements):>5} requirements "
            f"(expected {entry.expected_requirements}), "
            f"{len(result.context_chunks):>5} context chunks, "
            f"{len(result.extraction.misses)} miss(es)"
        )

    artifacts.append(
        write_extraction_report(manifest, results, paths.extraction_report_path(manifest))
    )
    spot_path, picks = write_spot_check(manifest, results, paths.spot_check_path(manifest))
    artifacts.append(spot_path)
    report.line(f"      wrote {artifacts[0]}")
    report.line(f"      wrote {spot_path}")

    # Ruling R26: the report is on disk, so now the warnings may be fatal. This
    # is the *only* place a duplicate id is fatal — the SQLite upsert cannot
    # refuse one without breaking idempotent re-ingestion.
    warnings = [
        f"{result.entry.key}: {warning}"
        for result in results
        for warning in result.extraction.warnings
    ]
    warnings.extend(cross_document_duplicates(results))
    if warnings:
        raise IngestionError(
            f"{len(warnings)} extraction warning(s) — refusing to index a corpus the "
            f"extractor is unsure about. The full report was written to {artifacts[0]} "
            "first, so nothing is lost:\n  " + "\n  ".join(warnings)
        )

    document_summaries = [
        DocumentSummary(
            key=result.entry.key,
            filename=result.entry.filename,
            module=result.entry.module,
            fetch_action=fetched.action,
            requirements=len(result.extraction.requirements),
            expected=result.entry.expected_requirements,
            context_chunks=len(result.context_chunks),
            misses=len(result.extraction.misses),
            warnings=len(result.extraction.warnings),
        )
        for result, fetched in zip(results, fetches, strict=True)
    ]

    if not opts.runs("code"):
        return RunSummary(
            project_id=manifest.project_id,
            documents=document_summaries,
            artifacts=artifacts,
            spot_checks=picks,
            elapsed_seconds=time.perf_counter() - started,
        )

    # -- 4. the code snapshot ---------------------------------------------
    report.stage("code", f"fetching {manifest.code.repo_url} at {manifest.code.git_sha[:12]}")
    repo_target = repo_dir(manifest)
    repo = fetch_repo(
        manifest,
        repo_target,
        runner=git,
        on_progress=lambda step, done, total: report.line(f"      [{done}/{total}] {step} ..."),
        force=opts.force,
    )
    report.line(f"      {repo.action:<14}sha={repo.sha}  {repo.file_count} file(s) in scope")
    index = index_repo(manifest, repo_target, on_progress=report.ticker("indexed"))
    kinds = Counter(unit.kind for unit in index.units)
    code_summary = CodeSummary(
        fetch_action=repo.action,
        git_sha=repo.sha,
        files=len(index.files),
        units=len(index.units),
        kinds=dict(kinds),
        annotations=index.annotations,
        claimed=index.claimed,
        not_claimed=index.not_claimed,
        distinct_ids=len(index.canonical_ids),
        partial_files=len(index.partial_files),
    )
    report.line(
        f"      {code_summary.units} code unit(s), {code_summary.annotations} annotation(s) "
        f"(@req {code_summary.claimed} + !req {code_summary.not_claimed})"
    )
    for warning in index.warnings[:5]:
        report.line(f"      note: {warning}")
    if len(index.warnings) > 5:
        report.line(f"      note: ... and {len(index.warnings) - 5} more annotation warning(s)")

    if not opts.runs("store"):
        return RunSummary(
            project_id=manifest.project_id,
            documents=document_summaries,
            code=code_summary,
            artifacts=artifacts,
            spot_checks=picks,
            elapsed_seconds=time.perf_counter() - started,
        )

    # -- 5. SQLite --------------------------------------------------------
    report.stage("store", f"writing {db_file}")
    conn = _connect(db_file)
    try:
        for result in results:
            db.bulk_insert_requirements(conn, result.extraction.requirements)
            db.bulk_insert_requirements(conn, result.context_chunks)
            # Measurable only while the PDF is open, needed by every citation
            # long afterwards (spec §6's `page_count`).
            db.put_document_meta(
                conn,
                manifest.project_id,
                result.entry.key,
                page_count=result.page_count,
            )
        for position, unit in enumerate(index.units, start=1):
            db.upsert_code_unit(conn, unit)
            if position % 250 == 0 or position == len(index.units):
                report.line(f"      stored {position}/{len(index.units)} code units")
        join = tier1_join(conn, manifest, index)
        report.line(
            "      "
            + ", ".join(f"{table} {count}" for table, count in _row_counts(conn, manifest).items())
        )

        # -- 6. embeddings + Chroma ---------------------------------------
        embed_summary = EmbedSummary(skipped=True, reason="stage not reached")
        if opts.runs("embed"):
            if opts.skip_embeddings:
                embed_summary = EmbedSummary(
                    skipped=True,
                    reason="--skip-embeddings: SQLite and BM25 are complete, Chroma is not",
                )
                report.stage("embed", "skipped (--skip-embeddings)")
            else:
                embed_summary = _embed_stage(
                    manifest, results, index, conn, opts, report, transport
                )

        # Counted after the embed stage, so `embedding cache` reflects what this
        # run actually left behind rather than what it started with.
        rows = _row_counts(conn, manifest)
        check = _index_check(conn, manifest, results, report)
        return RunSummary(
            project_id=manifest.project_id,
            documents=document_summaries,
            code=code_summary,
            join=join,
            rows=rows,
            embeddings=embed_summary,
            index_check=check,
            artifacts=artifacts,
            spot_checks=picks,
            elapsed_seconds=time.perf_counter() - started,
        )
    finally:
        conn.close()


def _embed_stage(
    manifest: ProjectManifest,
    results: Sequence[DocumentIngestion],
    index: RepoIndexResult,
    conn: sqlite3.Connection,
    opts: Options,
    report: Reporter,
    transport: Transport | None,
) -> EmbedSummary:
    """Embed every chunk (cache-aware) and upsert it into Chroma."""
    units, dropped = embeddable_code_units(index.units)
    documents = _index_documents(manifest, results, units)
    report.stage(
        "embed",
        f"{len(documents)} chunk(s) via {manifest.models.embedding} "
        f"(batch {opts.batch_size})",
    )

    client = client_for(manifest, transport=transport, batch_size=opts.batch_size)
    embedded = client.embed(
        [document.text for document in documents],
        conn,
        on_progress=report.ticker("embedded", steps=20),
    )
    report.line(
        f"      {embedded.computed} computed, {embedded.cached} from cache, "
        f"{embedded.duplicates} in-request duplicate(s)"
    )

    chroma_path = opts.chroma_path if opts.chroma_path is not None else paths.chroma_dir(manifest)
    client_api = vector_store.open_client(chroma_path)
    collection = vector_store.open_collection(client_api, manifest.project_id)
    upserted = vector_store.upsert(collection, documents, embedded.vectors)
    count = collection.count()
    report.line(f"      upserted {upserted} into {chroma_path} (collection holds {count})")

    return EmbedSummary(
        chunks=len(documents),
        computed=embedded.computed,
        cached=embedded.cached,
        duplicates=embedded.duplicates,
        upserted=upserted,
        collection_count=count,
        prototypes_dropped=dropped,
        usage=embedded.usage,
    )


def _index_check(
    conn: sqlite3.Connection,
    manifest: ProjectManifest,
    results: Sequence[DocumentIngestion],
    report: Reporter,
) -> IndexCheck:
    """Build the BM25 index the way the API does, time it, and query it."""
    started = time.perf_counter()
    index = bm25.build_from_sqlite(conn, manifest)
    elapsed = time.perf_counter() - started
    query, hits = _smoke_query(index, results)
    report.line(
        f"      bm25: {len(index)} record(s) indexed from SQLite in {elapsed:.2f}s"
    )
    if query is not None:
        top = f"{hits[0].record.id} (score {hits[0].score:.2f})" if hits else "no hits"
        report.line(f"      smoke query {query!r} -> {len(hits)} hit(s), top {top}")
    return IndexCheck(
        records=len(index),
        elapsed_seconds=elapsed,
        query=query,
        top_id=hits[0].record.id if hits else None,
        top_score=hits[0].score if hits else 0.0,
        hits=len(hits),
    )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ingestion.run",
        description=(
            "Fetch, parse, index and embed a corpus described by a project manifest. "
            "Idempotent: re-running downloads nothing and embeds nothing."
        ),
    )
    parser.add_argument("manifest", help="path to projects/<name>/project.yaml")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-download the PDFs and re-check out the repository even if present",
    )
    parser.add_argument(
        "--skip-embeddings",
        action="store_true",
        help="build SQLite and the BM25 index but no vectors (no OPENROUTER_API_KEY needed)",
    )
    parser.add_argument(
        "--stop-after",
        choices=STAGES,
        default=STAGES[-1],
        help="run the pipeline up to and including this stage (development aid)",
    )
    parser.add_argument("--db", default=None, help="override the SQLite path")
    parser.add_argument("--chroma", default=None, help="override the Chroma store directory")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"embedding inputs per request (default {DEFAULT_BATCH_SIZE})",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress progress lines")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code; never raises."""
    args = build_parser().parse_args(argv)
    reporter = Reporter(quiet=args.quiet)
    options = Options(
        force=args.force,
        skip_embeddings=args.skip_embeddings,
        stop_after=args.stop_after,
        db_path=Path(args.db).resolve() if args.db else None,
        chroma_path=Path(args.chroma).resolve() if args.chroma else None,
        batch_size=args.batch_size,
    )
    try:
        manifest = load_manifest(args.manifest)
        summary = run_ingestion(manifest, options, reporter=reporter)
    except KeyboardInterrupt:
        print("\ninterrupted — nothing is corrupted; re-run to resume", file=sys.stderr)
        return 130
    except (
        AnnotationError,
        CodeFetchError,
        EmbeddingError,
        ExtractionReportError,
        FetchError,
        IngestionError,
        ManifestError,
        OSError,
        ValueError,
        paths.PathError,
        vector_store.VectorStoreError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(render_summary(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
