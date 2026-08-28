"""Traceability reports: scope, cost, the run loop, the matrix (F4.2, S4.3.1).

A report is the evidence engine applied to every requirement in a scope, plus
the three things that turns it from a loop into a product: a cost estimate
before it starts, a hard stop while it runs, and a matrix that traces
SRS → SWS → verdict → evidence when it finishes.

**The cost model is not decoration.** Judging is the only expensive thing this
application does (finding D2: ingestion is effectively free), and the scopes
that matter are 200–400 requirements. So:

* the estimate is computed from OpenRouter's own published prices
  (:mod:`core.pricing`) and from what this corpus actually contains — the real
  cheap-tier candidate count per requirement, and the real mean length of a
  code unit — rather than from a guess about either;
* verdicts already in the cache are excluded from the estimate, so re-running
  a scope after a small change quotes the small number it will really cost;
* the ceiling is checked **before** each judge call using the running observed
  mean, so a run stops *at* the limit rather than after the call that crossed
  it.

**An aborted run keeps its work.** Every verdict is written to the cache as it
is produced (:func:`engines.evidence.check_implementation`), so a run that
stops at the ceiling has still bought 167 verdicts and the next run starts
from them. The canned conversation in ``frontend/lib/fixtures`` already tells
the user exactly that; this is the code that makes it true.

**Progress is pushed, not polled.** ``on_progress`` is called once per
requirement with ``{done, total, current}`` — spec §6's SSE payload — plus the
running per-status tallies and the spend so far, so the API layer streams a
live coverage picture without knowing anything about how a report is produced.
"""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core import db, pricing
from core.config import Settings
from core.llm import Llm
from core.manifest import ProjectManifest
from core.models import Requirement
from engines import evidence
from engines.evidence import VERDICT_STATUSES as STATUSES
from engines.evidence import EvidenceItem, Verdict
from retrieval.lookup import MAX_ID_LENGTH, InvalidRequirementId, lookup
from retrieval.pipeline import Engine

#: Characters per token, **measured on this corpus** rather than taken from
#: the usual "about four" rule of thumb.
#:
#: Four was the first value here and it under-quoted every run by about 45%:
#: a 324-requirement CanIf report estimated at $0.0950 cost $0.1371 (1.44x),
#: and the two judge-evaluation runs came in at 1.50x and 1.41x. Three
#: independent runs agreeing that closely is a constant, not noise — C source
#: is punctuation-dense and tokenises far below prose. 2.8 is 4.0 divided by
#: the mean of those three ratios.
#:
#: It also absorbs two smaller things the estimate does not model separately,
#: and deliberately so rather than growing a second fudge factor: a judge
#: reply that needed a retry (``Llm.structured``) is paid for twice, and the
#: tier-2 semantic pass embeds a query per requirement.
#:
#: Only the *estimate* depends on this. Actual cost always comes from
#: OpenRouter's own reported figure (finding D1), so an error here shows up as
#: a quote that was wrong, never as a bill that was.
CHARS_PER_TOKEN = 2.8

#: Completion tokens a verdict costs. Measured shape rather than measured
#: corpus: the reply is one small JSON object — a status, a confidence, a
#: sentence, and a handful of short rationales.
ESTIMATED_COMPLETION_TOKENS = 180

#: Statuses in the order a coverage table should read them.


class ScopeError(ValueError):
    """A scope that names something this corpus does not contain.

    Separate from an empty result: "module Foo" is the caller's mistake and
    becomes a 400, while "module Can, which has 240 requirements and no code"
    is a perfectly good report.
    """


# --------------------------------------------------------------------------
# scope
# --------------------------------------------------------------------------


#: Longest ``module``/``document`` selector accepted. Both are manifest keys
#: or module names — a dozen characters in this corpus — so this is generous.
MAX_SELECTOR_CHARS = 64

#: Most requirement ids one report may list explicitly. Above the size of the
#: ingested corpus, so no legitimate scope is refused, and bounded so a body
#: cannot be an arbitrary-length list.
MAX_SCOPE_IDS = 2000

#: Most module names one ``modules`` scope may list. A corpus has modules in
#: the single digits (this one has four documents), so the cap exists only to
#: bound the body — the same reason :data:`MAX_SCOPE_IDS` exists.
MAX_SCOPE_MODULES = 32

#: One requirement id in a scope, held to the same length limit
#: :func:`retrieval.lookup.lookup` enforces. The *shape* is not constrained
#: here on purpose: `lookup` normalises loose spellings ("sws can 11") and
#: reports the ones it cannot read, and a stricter regex at the edge would
#: reject ids the engine can actually resolve.
type ScopeId = Annotated[str, StringConstraints(max_length=MAX_ID_LENGTH)]

#: One module name in a ``modules`` scope, held to the same length limit a
#: single ``module`` selector gets.
type ScopeModule = Annotated[str, StringConstraints(max_length=MAX_SELECTOR_CHARS)]


class ReportScope(BaseModel):
    """What to report on.

    The four selectors are mutually exclusive and checked in the order
    ``req_ids``, ``modules``, ``module``, ``document``; none of them means the
    whole corpus. ``module`` is the one the agent and the report drawer use,
    and it is resolved through the manifest — ``CanIf`` names a *document's*
    module, never a hard-coded list. ``modules`` is the same resolution over a
    list, for a report that spans part of the corpus without being all of it.
    """

    model_config = ConfigDict(extra="forbid")

    #: Both are matched against the manifest, so neither can name anything
    #: outside the corpus — the caps are here so an absurd body is refused at
    #: the edge with a 422 rather than carried to the matcher (story S6.3.1).
    module: str | None = Field(default=None, max_length=MAX_SELECTOR_CHARS)
    #: Several modules in one report: the union of what each would select, in
    #: document order. Kept separate from ``module`` so the agent tool's
    #: single-module scope stays exactly what it always was.
    modules: list[ScopeModule] | None = Field(default=None, max_length=MAX_SCOPE_MODULES)
    document: str | None = Field(default=None, max_length=MAX_SELECTOR_CHARS)
    #: Each id is bounded by the same limit ``lookup`` enforces, and the list
    #: by :data:`MAX_SCOPE_IDS` — a set larger than a corpus is a module or a
    #: document, which are the selectors that exist for it.
    req_ids: list[ScopeId] | None = Field(default=None, max_length=MAX_SCOPE_IDS)
    #: Ignore cached verdicts and judge everything again. The expensive
    #: option, and off by default for that reason.
    rejudge: bool = False
    #: Stop after this many requirements. For a cheap demonstration run and
    #: for story S4.4.2's scoped evaluation subset.
    limit: int | None = Field(default=None, ge=1)

    def label(self) -> str:
        """A short human description, for a job list and an export header."""
        if self.req_ids:
            return f"{len(self.req_ids)} requirement(s)"
        if self.modules:
            return f"modules {', '.join(self.modules)}"
        if self.module:
            return f"module {self.module}"
        if self.document:
            return f"document {self.document}"
        return "the whole corpus"


def resolve_scope(
    engine: Engine, scope: ReportScope
) -> tuple[list[Requirement], list[str]]:
    """The requirements ``scope`` selects, and any ids it named that do not exist.

    Unknown ids are returned rather than raised on: a user pasting a list of
    twelve requirement ids should get a report on the eleven that exist and be
    told about the twelfth, not a 400 for the whole batch.
    """
    unknown: list[str] = []

    if scope.req_ids is not None:
        requirements: list[Requirement] = []
        for raw_id in scope.req_ids:
            try:
                found = lookup(engine.conn, engine.project_id, raw_id)
            except InvalidRequirementId:
                found = None
            if found is None:
                unknown.append(raw_id)
            else:
                requirements.append(found)
    else:
        requirements = db.list_requirements(
            engine.conn, engine.project_id, source_docs=_source_docs(engine.manifest, scope)
        )

    if scope.limit is not None:
        requirements = requirements[: scope.limit]
    return requirements, unknown


def _source_docs(manifest: ProjectManifest, scope: ReportScope) -> list[str] | None:
    """The manifest document keys a module/modules/document scope selects.

    A ``modules`` scope is the union of what each name selects, deduplicated —
    the same name twice, or in two spellings, must not double a document. One
    unknown name fails the whole scope: unlike an unknown *id*, which costs a
    row, an unknown module silently drops an entire document's worth of rows,
    and nobody reads a coverage matrix closely enough to notice that.
    """
    selected = scope.modules or ([scope.module] if scope.module else None)
    if selected:
        keys: list[str] = []
        for module in selected:
            matched = [
                document.key
                for document in manifest.documents
                if document.module.casefold() == module.casefold()
            ]
            if not matched:
                known = ", ".join(sorted({d.module for d in manifest.documents}))
                raise ScopeError(f"no module {module!r} in this corpus (known: {known})")
            keys.extend(key for key in matched if key not in keys)
        return keys
    if scope.document:
        keys = [d.key for d in manifest.documents if d.key == scope.document]
        if not keys:
            known = ", ".join(d.key for d in manifest.documents)
            raise ScopeError(f"no document {scope.document!r} in this corpus (known: {known})")
        return keys
    return None


def module_of(manifest: ProjectManifest, source_doc: str) -> str:
    """The module name for a manifest document key (``can_interface`` → ``CanIf``)."""
    for document in manifest.documents:
        if document.key == source_doc:
            return document.module
    return source_doc


# --------------------------------------------------------------------------
# cost (story S4.2.2)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CostEstimate:
    """What a run is expected to cost, and how that was arrived at.

    ``usd`` is ``None`` when no price could be read — the catalogue was
    unreachable, or does not list this model. The UI must then say "estimate
    unavailable" and show ``to_judge``; an invented figure next to a Launch
    button is worse than no figure.
    """

    requirements: int
    to_judge: int
    cached: int
    usd: float | None
    basis: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


def ceiling_usd(manifest: ProjectManifest, settings: Settings) -> float:
    """The hard stop, resolving the two places it is configured.

    ``MAX_REPORT_COST_USD`` in the environment is the operator's override and
    wins when it is set (spec §11 names it as *the* hard stop); the manifest's
    ``limits.max_report_cost_usd`` is the corpus default and applies otherwise.
    Both currently say $2, so the rule matters only once they disagree — which
    is exactly when an unstated precedence would cost somebody money.
    """
    if settings.max_report_cost_usd is not None:
        return settings.max_report_cost_usd
    return manifest.limits.max_report_cost_usd


def estimate_cost(
    engine: Engine,
    judge_model_id: str,
    requirements: Sequence[Requirement],
    *,
    rejudge: bool = False,
    price: pricing.ModelPrice | None = None,
) -> CostEstimate:
    """What judging ``requirements`` should cost, before anything is spent.

    The token count is built from things this corpus actually contains rather
    than from a round number: the judge's own system prompt, each
    requirement's own text, and the real number of cheap-tier candidates it
    has (an exact SQL count — no embeddings, nothing paid for).
    """
    git_sha = engine.manifest.code.git_sha
    to_judge = [
        requirement
        for requirement in requirements
        if rejudge
        or db.get_verdict(
            engine.conn,
            Requirement.canonical_id(requirement.id),
            git_sha,
            judge_model_id,
        )
        is None
    ]

    mean_candidate_chars = _mean_candidate_chars(engine)
    prompt_tokens = 0
    for requirement in to_judge:
        candidates, _, _ = evidence.gather_candidates(
            engine, requirement, include_semantic=False
        )
        expected = min(
            evidence.MAX_CANDIDATES, len(candidates) + evidence.SEMANTIC_TOP_N
        )
        chars = (
            len(evidence.SYSTEM_PROMPT)
            + len(requirement.text)
            + expected * mean_candidate_chars
        )
        prompt_tokens += int(chars / CHARS_PER_TOKEN)
    completion_tokens = len(to_judge) * ESTIMATED_COMPLETION_TOKENS

    resolved = price if price is not None else pricing.price_of(judge_model_id)
    if resolved is None:
        reason = pricing.failure_reason() or f"{judge_model_id} is not in the model catalogue"
        return CostEstimate(
            requirements=len(requirements),
            to_judge=len(to_judge),
            cached=len(requirements) - len(to_judge),
            usd=None,
            basis=f"estimate unavailable — {reason}",
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    return CostEstimate(
        requirements=len(requirements),
        to_judge=len(to_judge),
        cached=len(requirements) - len(to_judge),
        usd=resolved.cost_of(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
        basis=f"openrouter catalogue prices for {judge_model_id}",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def _mean_candidate_chars(engine: Engine) -> float:
    """Mean length of a candidate as the judge sees it — clipped, like it is."""
    row = engine.conn.execute(
        "SELECT AVG(MIN(LENGTH(text), ?)) AS mean FROM code_units WHERE project_id = ?",
        (evidence.MAX_CANDIDATE_CHARS, engine.project_id),
    ).fetchone()
    return float(row["mean"] or 0.0)


# --------------------------------------------------------------------------
# the matrix
# --------------------------------------------------------------------------


class ReportRow(BaseModel):
    """One requirement's row of the traceability matrix.

    ``upstream_ids`` is the SRS side of SRS → SWS → code: the SRS documents
    are not ingested (spec §11 makes them cite-only), so a row traces *up* to
    an id and *down* to a file, and the middle is the requirement itself.
    """

    model_config = ConfigDict(extra="forbid")

    req_id: str
    #: The requirement's own words — a matrix is read without the PDF open.
    #: Defaulted so results stored before the field existed still validate.
    text: str = ""
    doc: str
    module: str
    section: str | None
    page: int
    upstream_ids: list[str] = Field(default_factory=list)
    status: str
    confidence: float
    rationale: str
    evidence: list[EvidenceItem] = Field(default_factory=list)
    #: True when this row came from the verdict cache and cost nothing.
    cached: bool = False


class Coverage(BaseModel):
    """Counts over a finished (or abandoned) run."""

    model_config = ConfigDict(extra="forbid")

    total: int
    judged: int
    from_cache: int
    implemented: int = 0
    partial: int = 0
    missing: int = 0
    unverifiable: int = 0
    #: Rows the judge backed with at least one cited code unit. Distinct from
    #: ``covered``: a verdict is a judgement and this is what it pointed at,
    #: so a row can be ``implemented`` with nothing cited (the judge was sure
    #: but named nothing) and the gap between the two numbers is worth seeing.
    #: It is **not** a count of developer ``@req`` annotations — those are an
    #: input to the judge, not its output.
    with_evidence: int = 0

    @property
    def covered(self) -> int:
        """Requirements with any implementation evidence at all."""
        return self.implemented + self.partial

    @property
    def covered_pct(self) -> float:
        return 100.0 * self.covered / self.judged if self.judged else 0.0


class ReportResult(BaseModel):
    """A finished traceability report: the matrix, the stats, the bill."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    scope: ReportScope
    scope_label: str
    git_sha: str
    judge_model_id: str
    corpus_version: str
    rows: list[ReportRow] = Field(default_factory=list)
    coverage: Coverage
    cost_usd: float = 0.0
    ceiling_usd: float = 0.0
    elapsed_ms: int = 0
    #: Ids the scope named that this corpus does not contain.
    unknown_ids: list[str] = Field(default_factory=list)
    #: True when the run stopped at ``ceiling_usd`` before finishing.
    aborted: bool = False
    abort_reason: str | None = None


# --------------------------------------------------------------------------
# the run loop (story S4.2.1)
# --------------------------------------------------------------------------

#: ``(done, total, current_req_id, verdict_counts, spent_usd)`` — spec §6's
#: ``{done, total, current}`` plus one running count per verdict status and
#: the spend so far, so a progress bar can show coverage forming rather than
#: only a number climbing.
ProgressFn = Callable[[int, int, str, dict[str, int], float], None]


def run_report(
    engine: Engine,
    judge_llm: Llm,
    scope: ReportScope,
    *,
    ceiling: float,
    on_progress: ProgressFn | None = None,
    should_stop: Callable[[], bool] | None = None,
    blind: bool = False,
) -> ReportResult:
    """Judge every requirement in ``scope``, stopping at ``ceiling``.

    Never raises for a corpus reason. One requirement that cannot be judged
    becomes an ``unverifiable`` row (:mod:`engines.evidence` guarantees that),
    because ending a 398-row run over one bad row would throw away every
    verdict paid for before it.

    ``should_stop`` lets the API cancel a run between requirements.

    ``blind`` is story S4.4.2's measurement mode and is never used by the
    product — see :func:`engines.evidence.judge`.
    """
    started = time.perf_counter()
    requirements, unknown = resolve_scope(engine, scope)
    total = len(requirements)

    rows: list[ReportRow] = []
    counts = {status: 0 for status in STATUSES}
    spent = 0.0
    judged_calls = 0
    aborted = False
    reason: str | None = None

    for index, requirement in enumerate(requirements, start=1):
        if should_stop is not None and should_stop():
            aborted, reason = True, "The run was cancelled."
            break

        projected = spent + (spent / judged_calls if judged_calls else 0.0)
        if judged_calls and projected > ceiling:
            aborted = True
            reason = (
                f"The run reached MAX_REPORT_COST_USD (${ceiling:.2f}) after judging "
                f"{len(rows)} of {total} requirements. The {len(rows)} verdicts it "
                "already produced are kept, so re-running will reuse them."
            )
            break

        check = evidence.check_implementation(
            engine, judge_llm, requirement.id, use_cache=not scope.rejudge, blind=blind
        )
        spent += check.cost_usd
        if not check.cached:
            judged_calls += 1
        row = _row(engine.manifest, requirement, check.verdict, cached=check.cached)
        rows.append(row)
        counts[row.status] += 1
        if on_progress is not None:
            # A copy: the API hands this to the SSE reader on another thread,
            # which must not see later requirements mutate it.
            on_progress(index, total, requirement.id, dict(counts), spent)

    return ReportResult(
        project_id=engine.project_id,
        scope=scope,
        scope_label=scope.label(),
        git_sha=engine.manifest.code.git_sha,
        judge_model_id=judge_llm.model_id,
        corpus_version=engine.manifest.version,
        rows=rows,
        coverage=coverage_of(rows, total=total),
        cost_usd=spent,
        ceiling_usd=ceiling,
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        unknown_ids=unknown,
        aborted=aborted,
        abort_reason=reason,
    )


def _row(
    manifest: ProjectManifest, requirement: Requirement, verdict: Verdict, *, cached: bool
) -> ReportRow:
    return ReportRow(
        req_id=requirement.id,
        text=requirement.text,
        doc=requirement.source_doc,
        module=module_of(manifest, requirement.source_doc),
        section=requirement.section_path,
        page=requirement.page,
        upstream_ids=list(requirement.upstream_ids),
        status=verdict.status,
        confidence=verdict.confidence,
        rationale=verdict.rationale,
        evidence=list(verdict.evidence),
        cached=cached,
    )


def coverage_of(rows: Sequence[ReportRow], *, total: int) -> Coverage:
    counts = {status: 0 for status in STATUSES}
    for row in rows:
        counts[row.status] = counts.get(row.status, 0) + 1
    return Coverage(
        total=total,
        judged=len(rows),
        from_cache=sum(1 for row in rows if row.cached),
        with_evidence=sum(1 for row in rows if row.evidence),
        **counts,
    )


# --------------------------------------------------------------------------
# export (story S4.3.1)
# --------------------------------------------------------------------------

EXPORT_FORMATS = ("md", "csv", "json")

MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
    "json": "application/json",
}


def export(result: ReportResult, fmt: str) -> str:
    if fmt == "json":
        return result.model_dump_json(indent=2)
    if fmt == "csv":
        return to_csv(result)
    if fmt == "md":
        return to_markdown(result)
    raise ValueError(f"unknown export format {fmt!r}; expected one of {', '.join(EXPORT_FORMATS)}")


def to_csv(result: ReportResult) -> str:
    """The matrix as CSV — one row per requirement, evidence flattened.

    Evidence is joined into one cell rather than exploded into extra rows: a
    spreadsheet of this is filtered and pivoted on requirement, and a
    requirement occupying three rows breaks every count someone does to it.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "req_id",
            "text",
            "module",
            "document",
            "section",
            "page",
            "upstream_ids",
            "status",
            "confidence",
            "evidence",
            "rationale",
        ]
    )
    for row in result.rows:
        writer.writerow(
            [
                row.req_id,
                row.text,
                row.module,
                row.doc,
                row.section or "",
                row.page,
                " ".join(row.upstream_ids),
                row.status,
                f"{row.confidence:.2f}",
                " ".join(_evidence_ref(item) for item in row.evidence),
                row.rationale,
            ]
        )
    return buffer.getvalue()


def to_markdown(result: ReportResult) -> str:
    """The matrix as a readable document, header first.

    The header is not garnish: a verdict is only meaningful against the
    snapshot and the judge model that produced it, and a matrix pasted into a
    review without those is unfalsifiable.
    """
    coverage = result.coverage
    lines = [
        f"# Traceability report — {result.scope_label}",
        "",
        f"- Corpus: `{result.project_id}` {result.corpus_version}",
        f"- Code snapshot: `{result.git_sha}`",
        f"- Judge model: `{result.judge_model_id}`",
        f"- Cost: ${result.cost_usd:.4f} of a ${result.ceiling_usd:.2f} ceiling",
        "",
        "## Coverage",
        "",
        "| status | requirements |",
        "| --- | ---: |",
    ]
    lines.extend(
        f"| {status} | {getattr(coverage, status)} |" for status in STATUSES
    )
    lines.extend(
        [
            f"| **judged** | **{coverage.judged} of {coverage.total}** |",
            "",
            f"Implemented or partial: {coverage.covered} of {coverage.judged} judged "
            f"({coverage.covered_pct:.1f}%). {coverage.from_cache} verdict(s) came from "
            "the cache and cost nothing.",
            "",
        ]
    )

    if result.aborted and result.abort_reason:
        lines.extend([f"> **Run incomplete.** {result.abort_reason}", ""])
    if result.unknown_ids:
        lines.extend(
            [
                "> Not in this corpus, and therefore not reported on: "
                + ", ".join(f"`{one}`" for one in result.unknown_ids),
                "",
            ]
        )

    lines.extend(
        [
            "## Matrix",
            "",
            "| SRS (upstream) | SWS requirement | text | verdict | confidence | evidence |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
    )
    for row in result.rows:
        upstream = ", ".join(f"`{one}`" for one in row.upstream_ids) or "—"
        evidence_cell = (
            ", ".join(f"`{_evidence_ref(item)}`" for item in row.evidence) or "—"
        )
        lines.append(
            f"| {upstream} | `{row.req_id}` | {_text_cell(row.text)} | {row.status} | "
            f"{row.confidence:.2f} | {evidence_cell} |"
        )
    return "\n".join(lines) + "\n"


#: Characters of requirement text a markdown matrix cell keeps. Only the
#: table is clipped — the CSV and JSON exports carry the full text — because
#: a table column as wide as a requirement is unreadable, not because the
#: text is dispensable.
MARKDOWN_TEXT_CHARS = 120


def _text_cell(text: str) -> str:
    """Requirement text as one table cell: one line, pipes escaped, clipped."""
    flat = " ".join(text.split()).replace("|", "\\|")
    if len(flat) > MARKDOWN_TEXT_CHARS:
        flat = flat[: MARKDOWN_TEXT_CHARS - 1].rstrip() + "…"
    return flat or "—"


def _evidence_ref(item: EvidenceItem) -> str:
    start, end = item.lines
    return f"{item.file}:{start}-{end}"
