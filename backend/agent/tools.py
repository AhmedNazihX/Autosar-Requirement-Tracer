"""The agent's tools, and the registry WP4 plugs into (story S3.2.1).

Each tool wraps a WP2 engine and produces **two** outputs, because the model
and the UI need different things and neither can be derived from the other:

* **A text payload the model reads.** Every piece of retrieved document or code
  text in it is fenced and framed as data (spec §8). Tool payloads are exactly
  the channel a poisoned document arrives through, and the model is the one
  component that can be talked into acting.
* **A side channel the SSE stream reads** — structured citations, the RAG stage
  log, usage, and the chip's one-line summary. This cannot go through the model:
  spec §6 requires citations to be structured events, never parsed back out of
  prose, because a model that paraphrases an id produces a citation that points
  at the wrong requirement and looks fine.

The side channel is keyed by ``tool_call_id`` (LangChain injects it via
``InjectedToolCallId``), which is the same id the ``tool_start`` /
``tool_result`` events correlate on — so two calls to the same tool in one turn
stay separate.

**A tool never raises.** A tool that throws kills the turn; a tool that returns
"this failed, here is why" lets the model tell the user what happened and carry
on. So every tool body is wrapped, and the failure is recorded as ``ok=False``
with a readable sentence — never a traceback, which would leak internals into a
prompt and then possibly into an answer.

The registry is a plain dict of builders. CLAUDE.md caps the abstraction here
deliberately: WP4's story S4.3.2 adds two entries to it, and that is the whole
extension mechanism.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any

from langchain_core.tools import BaseTool, InjectedToolCallId, StructuredTool

from api.chat_events import CodeCitation, RagStage, RequirementCitation, UpstreamCitation
from core import db
from core.llm import Llm
from core.models import CodeUnit, Requirement
from engines import evidence
from engines.report import ReportScope, ScopeError
from retrieval import pipeline
from retrieval.lookup import InvalidRequirementId, lookup
from retrieval.pipeline import Engine, PipelineUsage
from retrieval.prompting import fence
from retrieval.self_query import QueryFilter

#: Fences for the two kinds of untrusted text a tool can show the model.
#: Named constants so story S6.1.1 can assert on them rather than on prose.
#: The wrapping itself is :func:`retrieval.prompting.fence` — one fence in the
#: codebase, so there is one place to harden and one shape to assert on.
REQUIREMENT_FENCE = "<<<REQUIREMENT_TEXT>>>"
SOURCE_FENCE = "<<<SOURCE>>>"


#: Pipeline stages worth showing on the tool chip, in order, with the label the
#: user sees. ``hydrate`` is deliberately absent: loading rows out of SQLite is
#: plumbing, not a retrieval stage, and listing it would pad the advanced-RAG
#: story with something that is not part of it.
_STAGE_LABELS: dict[str, str] = {
    "expand": "multi-query",
    "self_query": "self-query",
    "retrieve": "hybrid search",
    "fuse": "RRF fusion",
    "rerank": "LLM rerank",
}

Citation = RequirementCitation | CodeCitation | UpstreamCitation


@dataclass(frozen=True)
class ToolOutcome:
    """Everything one tool call produced besides its model payload."""

    summary: str
    ok: bool = True
    error: str | None = None
    citations: tuple[Citation, ...] = ()
    stages: tuple[RagStage, ...] | None = None
    usage: PipelineUsage | None = None
    #: Set only by ``generate_traceability_report``: the launched run's id,
    #: forwarded on the ``tool_result`` event so the report drawer can attach.
    job_id: str | None = None


@dataclass
class SideChannel:
    """Per-turn record of each tool call's structured output, keyed by call id.

    One per turn, never module state: two concurrent requests share tool
    *definitions* but must never share outcomes.
    """

    _outcomes: dict[str, ToolOutcome] = field(default_factory=dict)

    def record(self, call_id: str, outcome: ToolOutcome) -> None:
        self._outcomes[call_id] = outcome

    def outcome(self, call_id: str) -> ToolOutcome | None:
        return self._outcomes.get(call_id)

    def all_citations(self) -> list[Citation]:
        """Every citation from the turn, in the order the calls recorded them."""
        return [c for outcome in self._outcomes.values() for c in outcome.citations]


#: What ``generate_traceability_report`` needs from the API layer: launch this
#: scope, return something with ``job_id``, ``to_judge`` and ``est_cost_usd``.
#:
#: A callable rather than an import, because the launcher lives in
#: :mod:`api.reports` and this module must not depend on FastAPI — the tools
#: are the agent's, not the web layer's, and every test that builds a
#: ``ToolContext`` would otherwise have to stand up an app.
ReportLauncher = Callable[[Any], Any]


@dataclass(frozen=True)
class ToolContext:
    """What the tools need: the retrieval engine, document facts, a side channel."""

    engine: Engine
    doc_titles: dict[str, str]
    page_counts: dict[str, int]
    side: SideChannel
    #: The tool-less judge for ``check_implementation`` (spec §8). ``None``
    #: when no model could be built — the tool then says so instead of the
    #: turn dying, which is the same rule every other tool follows.
    judge_llm: Llm | None = None
    #: Starts a background report for ``generate_traceability_report``.
    launch_report: ReportLauncher | None = None

    @classmethod
    def build(
        cls,
        engine: Engine,
        *,
        side: SideChannel | None = None,
        judge_llm: Llm | None = None,
        launch_report: ReportLauncher | None = None,
    ) -> ToolContext:
        """Read the per-document facts a citation needs, once per turn.

        Titles come from the manifest and page counts from SQLite (story
        S3.1.1's ``documents`` table). Both are read here rather than per
        citation — four rows, and a citation should not cost a query.
        """
        return cls(
            engine=engine,
            doc_titles={
                document.key: document.title for document in engine.manifest.documents
            },
            page_counts=db.page_counts(engine.conn, engine.project_id),
            side=side if side is not None else SideChannel(),
            judge_llm=judge_llm,
            launch_report=launch_report,
        )

    def citation_of(self, requirement: Requirement) -> RequirementCitation:
        """A wire citation for ``requirement``, with its document's facts filled in."""
        return RequirementCitation(
            req_id=requirement.id,
            doc=requirement.source_doc,
            doc_title=self.doc_titles.get(requirement.source_doc, requirement.source_doc),
            page=requirement.page,
            bbox=requirement.bbox,
            page_count=self.page_counts.get(requirement.source_doc, 0),
            section=requirement.section_path,
            quote=requirement.text,
        )

    def upstream_of(self, requirement: Requirement) -> list[UpstreamCitation]:
        """Dashed, never-clickable chips for the SRS/RS ids a requirement cites."""
        return [
            UpstreamCitation(
                req_id=upstream_id,
                cited_by=requirement.id,
                doc=_upstream_document(upstream_id),
            )
            for upstream_id in requirement.upstream_ids
        ]


def _upstream_document(upstream_id: str) -> str:
    """A display name for an upstream document, derived from its id.

    Display only: SRS documents are not ingested (spec §11 makes them
    cite-only), so there is no manifest key to use and nothing to open.
    """
    parts = upstream_id.split("_")
    return " ".join(parts[:2]) if len(parts) >= 2 else upstream_id


# --------------------------------------------------------------------------
# lookup_requirement
# --------------------------------------------------------------------------

_LOOKUP_DESCRIPTION = """\
Fetch one requirement by its exact identifier, e.g. SWS_Can_00011, \
SWS_CANIF_00023, sws_can_11. Use this whenever the user names a requirement id \
— it is an exact database lookup, never a search, so it is the only correct way \
to answer "what does <id> require?". Returns the requirement's verbatim text, \
its document, page and section, and the upstream requirements it traces to.\
"""



#: Most requirement chips one `search_code` answer may carry. A single unit can
#: be tied to dozens — 47 for `CanIf_RxIndication` in the real corpus — and a
#: wall of chips is no more usable than none. The order below puts the
#: best-evidenced first, so a cut here drops the weakest links.
MAX_LINKED_REQUIREMENTS = 5


def _requirements_behind(
    context: ToolContext, units: Sequence[CodeUnit]
) -> list[RequirementCitation]:
    """The requirements this code is tied to, best evidence first.

    The walk itself is :func:`engines.evidence.requirements_behind`, shared
    with the pane's ``GET /code/{path}/requirements`` so the two can never
    disagree. What is the *agent's* decision, and stays here: a ``!req`` link
    is deliberately never cited — it says the developers believe the
    requirement is *not* implemented here, so offering it under an answer
    about what this code does would invert its meaning. It remains visible in
    the pane's own list, labelled; this is about what the answer asserts.

    Free — SQL against the index, no model call.
    """
    claimed, anchored, _denied = evidence.requirements_behind(
        context.engine.conn, context.engine.project_id, units
    )
    ordered = [link.requirement for link in (*claimed, *anchored)]
    return [
        context.citation_of(requirement)
        for requirement in ordered[:MAX_LINKED_REQUIREMENTS]
    ]


def _lookup_tool(context: ToolContext) -> BaseTool:
    def lookup_requirement(
        req_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        try:
            requirement = lookup(context.engine.conn, context.engine.project_id, req_id)
        except InvalidRequirementId as exc:
            return _fail(context, tool_call_id, f"{req_id!r} is not a requirement id: {exc}")
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the turn
            return _fail(context, tool_call_id, _readable(exc))

        if requirement is None:
            context.side.record(tool_call_id, ToolOutcome(summary=f"{req_id}: not found"))
            return (
                f"There is no requirement {req_id!r} in this corpus. Do not guess at "
                "one; tell the user it is not in the ingested specifications."
            )

        citations: list[Citation] = [context.citation_of(requirement)]
        citations.extend(context.upstream_of(requirement))
        context.side.record(
            tool_call_id,
            ToolOutcome(summary=requirement.id, citations=tuple(citations)),
        )

        lines = [
            f"[{requirement.id}] {requirement.source_doc} page {requirement.page}",
        ]
        if requirement.section_path:
            lines.append(f"section: {requirement.section_path}")
        if requirement.title:
            lines.append(f"title: {requirement.title}")
        lines.append(fence(REQUIREMENT_FENCE, requirement.text, what="requirement text"))
        if requirement.upstream_ids:
            lines.append(f"traces up to: {', '.join(requirement.upstream_ids)}")
        if requirement.named_symbols:
            lines.append(f"names the C symbols: {', '.join(requirement.named_symbols)}")
        return "\n".join(lines)

    return StructuredTool.from_function(
        lookup_requirement,
        name="lookup_requirement",
        description=_LOOKUP_DESCRIPTION,
    )


# --------------------------------------------------------------------------
# search_requirements
# --------------------------------------------------------------------------

_SEARCH_DESCRIPTION = """\
Search the AUTOSAR specifications for requirements about a topic, by meaning as \
well as by keyword. Use this when the user describes a behaviour rather than \
naming an id. Optionally narrow by module (Can, CanIf, CanTp, CanSM) or by \
document section. Returns the most relevant requirements with their ids, \
documents, pages and verbatim text.

`top_n` defaults to 5, which suits "how does X work". When the user asks WHICH \
or WHAT requirements cover something — an enumeration, not an explanation — \
that default will silently under-answer them: pass a larger `top_n` (15-20) so \
the answer can list the whole set rather than a sample of it.

Results include normative requirements AND background context passages from \
the same documents. They are labelled, and they are not interchangeable — see \
the labels in the result.\
"""


def _search_requirements_tool(context: ToolContext) -> BaseTool:
    def search_requirements(
        query: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
        module: str | None = None,
        section: str | None = None,
        top_n: int = pipeline.DEFAULT_TOP_N,
    ) -> str:
        filters = (
            QueryFilter(module=module, section=section)
            if (module or section)
            else None
        )
        try:
            result = pipeline.search_requirements(
                context.engine, query, filters=filters, top_n=top_n
            )
        except ValueError as exc:
            return _fail(context, tool_call_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the turn
            return _fail(context, tool_call_id, _readable(exc))

        stages = _rag_stages(result.stages)
        # Only requirements become citation chips. Context prose is retrieved
        # and shown to the model — it genuinely helps answer "how does this
        # work?" — but its id is synthetic (`CTX_can_driver_6_14`), and the
        # frontend renders a chip as `[{req_id}]`, so citing it puts a
        # meaningless label in front of the user. Verified on the real corpus:
        # 2 of 5 chips for a bus-off question were CTX ids.
        citations = [
            context.citation_of(item.requirement)
            for item in result.results
            if item.requirement.doc_type == "requirement"
        ]
        candidates = _candidate_count(result.stages)
        context.side.record(
            tool_call_id,
            ToolOutcome(
                summary=f"{len(result.results)} of {candidates}",
                citations=tuple(citations),
                stages=tuple(stages),
                usage=result.usage,
            ),
        )

        if not result.results:
            return (
                f"No requirement in the ingested specifications matched {query!r}. "
                "Say so plainly rather than offering a guess."
            )

        # Requirements and context are labelled apart, and the header counts
        # them. Without this they render identically — `CTX_can_interface_7.8_01`
        # looks exactly like `SWS_CANIF_00085` — and the model presents context
        # prose as a requirement. Measured: asked which requirements cover CanIf
        # initialisation, it answered with two CTX ids as bulleted
        # "requirements", which carry no citation chip and mean nothing to a
        # reader.
        requirements = [
            item for item in result.results if item.requirement.doc_type == "requirement"
        ]
        passages = [
            item for item in result.results if item.requirement.doc_type != "requirement"
        ]

        blocks = [
            f"Found {len(requirements)} normative requirement(s) and "
            f"{len(passages)} background passage(s), best first."
        ]
        for item in result.results:
            requirement = item.requirement
            is_requirement = requirement.doc_type == "requirement"
            label = "REQUIREMENT" if is_requirement else "CONTEXT (not a requirement)"
            head = (
                f"{item.rank}. {label} [{requirement.id}] "
                f"{requirement.source_doc} page {requirement.page}"
            )
            if requirement.section_path:
                head += f" · {requirement.section_path}"
            if not is_requirement:
                head += (
                    "\n   Background prose, with a synthetic id. Use it to explain, "
                    "never cite it as a requirement and never print its id."
                )
            body = fence(REQUIREMENT_FENCE, requirement.text, what="requirement text")
            blocks.append(f"{head}\n{body}")

        # Keyed on the result count, not the requirement count: what the caller
        # needs to know is that the list was cut off at `top_n`, and it was cut
        # off across both kinds. Keying on requirements alone stays silent
        # exactly when context filled the slots — the case where the answer is
        # most likely to be short.
        if len(result.results) >= top_n:
            blocks.append(
                f"This is the top {top_n} by relevance, not the complete set — "
                "there may be more. If the user asked which requirements cover "
                "something, say the list may be incomplete or search again with a "
                "larger top_n."
            )
        return "\n\n".join(blocks)

    return StructuredTool.from_function(
        search_requirements,
        name="search_requirements",
        description=_SEARCH_DESCRIPTION,
    )


# --------------------------------------------------------------------------
# search_code
# --------------------------------------------------------------------------

_SEARCH_CODE_DESCRIPTION = """\
Search the pinned open-source C snapshot for implementation code. Pass `symbol` \
for an exact function or type name (an exact index lookup), or `query` to search \
by meaning. Returns matching functions with their file path, line span and \
source. Note the snapshot implements an older AUTOSAR release than the ingested \
specifications, so an absent implementation is expected and is not an error.\
"""


def _search_code_tool(context: ToolContext) -> BaseTool:
    def search_code(
        tool_call_id: Annotated[str, InjectedToolCallId],
        query: str | None = None,
        symbol: str | None = None,
        top_n: int = pipeline.DEFAULT_TOP_N,
    ) -> str:
        try:
            result = pipeline.search_code(
                context.engine, query=query, symbol=symbol, top_n=top_n
            )
        except ValueError as exc:
            return _fail(context, tool_call_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the turn
            return _fail(context, tool_call_id, _readable(exc))

        citations: list[Citation] = [
            CodeCitation(
                repo_path=item.unit.repo_path,
                symbol=item.unit.symbol,
                line_span=item.unit.line_span,
                git_sha=item.unit.git_sha,
            )
            for item in result.results
        ]
        # Code first, then the specification. Order is load-bearing: the source
        # pane opens on a turn's *first* citation
        # (`frontend/lib/source-target.ts`), and for "where is X defined?" the
        # code is the answer — the requirement is the context for it.
        citations.extend(
            _requirements_behind(context, [item.unit for item in result.results])
        )
        context.side.record(
            tool_call_id,
            ToolOutcome(
                summary=f"{len(result.results)} code unit(s)",
                citations=tuple(citations),
                usage=result.usage,
            ),
        )

        if not result.results:
            return (
                f"No code in the pinned snapshot matched {symbol or query!r}. The "
                "snapshot implements an older release, so this may simply not be "
                "implemented there — say that rather than implying a defect."
            )

        blocks = []
        for item in result.results:
            unit = item.unit
            start, end = unit.line_span
            head = f"{item.rank}. {unit.repo_path}:{start}-{end} ({unit.kind} {unit.symbol})"
            body = fence(SOURCE_FENCE, unit.text, what="C source")
            blocks.append(f"{head}\n{body}")
        return "\n\n".join(blocks)

    return StructuredTool.from_function(
        search_code,
        name="search_code",
        description=_SEARCH_CODE_DESCRIPTION,
    )


# --------------------------------------------------------------------------
# check_implementation (story S4.3.2)
# --------------------------------------------------------------------------

_CHECK_DESCRIPTION = """\
Check whether one requirement is implemented in the pinned C snapshot, and say \
what the evidence is. Pass the requirement's exact id. Returns a verdict — \
implemented, partial, missing or unverifiable — with the file and line span of \
each piece of supporting code and a short reason. Use this when the user asks \
whether something is implemented, where it is implemented, or for evidence \
about one requirement. The snapshot implements an older AUTOSAR release than \
the specifications, so `missing` is a common and expected answer rather than a \
fault.\
"""


def _check_implementation_tool(context: ToolContext) -> BaseTool:
    def check_implementation(
        req_id: str,
        tool_call_id: Annotated[str, InjectedToolCallId],
    ) -> str:
        if context.judge_llm is None:
            return _fail(
                context,
                tool_call_id,
                "no judge model is configured, so implementation cannot be checked",
            )
        try:
            check = evidence.check_implementation(
                context.engine, context.judge_llm, req_id
            )
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the turn
            return _fail(context, tool_call_id, _readable(exc))

        verdict = check.verdict
        citations: list[Citation] = []
        if check.requirement is not None:
            citations.append(context.citation_of(check.requirement))
        citations.extend(
            CodeCitation(
                repo_path=item.file,
                symbol=item.symbol,
                line_span=item.lines,
                git_sha=item.git_sha,
            )
            for item in verdict.evidence
        )
        context.side.record(
            tool_call_id,
            ToolOutcome(
                summary=f"{check.req_id}: {verdict.status}",
                citations=tuple(citations),
                usage=check.usage,
            ),
        )

        lines = [
            f"verdict for {check.req_id}: {verdict.status} "
            f"(confidence {verdict.confidence:.2f})",
            verdict.rationale,
        ]
        if verdict.evidence:
            lines.append("evidence:")
            lines.extend(
                f"- {item.file}:{item.lines[0]}-{item.lines[1]} ({item.symbol}) — "
                f"{item.rationale}"
                for item in verdict.evidence
            )
        else:
            lines.append(
                "No supporting code was identified in the pinned snapshot. Report "
                "that as a finding about release drift, not as a defect."
            )
        return "\n".join(lines)

    return StructuredTool.from_function(
        check_implementation,
        name="check_implementation",
        description=_CHECK_DESCRIPTION,
    )


# --------------------------------------------------------------------------
# generate_traceability_report (story S4.3.2)
# --------------------------------------------------------------------------

_REPORT_DESCRIPTION = """\
Start a traceability report over a scope of requirements: every requirement in \
scope is checked against the code and the result is a matrix of SRS → SWS → \
verdict → evidence with coverage statistics. Scope it with `module` (Can, \
CanIf, CanTp, CanSM), `document`, or an explicit list of `req_ids`; with none \
of those it covers the whole corpus, which is slow and expensive. Set `rejudge` \
to re-judge requirements that already have a cached verdict. Set `limit` for a \
quick partial run. This starts a background job and returns immediately with \
its id and a cost estimate — it does NOT return the finished matrix, so tell \
the user the report is running and that they can follow it in the report \
drawer. Never call this to answer a question about a single requirement; use \
check_implementation for that.\
"""


def _report_tool(context: ToolContext) -> BaseTool:
    def generate_traceability_report(
        tool_call_id: Annotated[str, InjectedToolCallId],
        module: str | None = None,
        document: str | None = None,
        req_ids: list[str] | None = None,
        rejudge: bool = False,
        limit: int | None = None,
    ) -> str:
        if context.launch_report is None:
            return _fail(
                context,
                tool_call_id,
                "reports cannot be started from this context",
            )
        try:
            scope = ReportScope(
                module=module,
                document=document,
                req_ids=req_ids,
                rejudge=rejudge,
                limit=limit,
            )
        except ValueError as exc:
            return _fail(context, tool_call_id, f"that scope is not valid: {exc}")

        try:
            launched = context.launch_report(scope)
        except ScopeError as exc:
            return _fail(context, tool_call_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the turn
            return _fail(context, tool_call_id, _readable(exc))

        cost = (
            f"about ${launched.est_cost_usd:.4f}"
            if launched.est_cost_usd is not None
            else f"an unknown amount ({launched.est_basis})"
        )
        context.side.record(
            tool_call_id,
            ToolOutcome(
                summary=f"{launched.to_judge} to judge · {cost}",
                job_id=launched.job_id,
            ),
        )

        lines = [
            f"Started a traceability report over {launched.scope_label}.",
            f"job id: {launched.job_id}",
            f"{launched.requirements} requirement(s) in scope; {launched.to_judge} "
            f"still to judge and {launched.cached} already cached.",
            f"estimated cost: {cost}, against a ${launched.ceiling_usd:.2f} ceiling.",
            "The job runs in the background — it is not finished yet, so do not "
            "describe its results. Tell the user it is running and that progress "
            "and the matrix appear in the report drawer.",
        ]
        if launched.unknown_ids:
            lines.append(
                "Not in this corpus, and therefore not included: "
                + ", ".join(launched.unknown_ids)
            )
        return "\n".join(lines)

    return StructuredTool.from_function(
        generate_traceability_report,
        name="generate_traceability_report",
        description=_REPORT_DESCRIPTION,
    )


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def _fail(context: ToolContext, call_id: str, message: str) -> str:
    """Record a failure and give the model a sentence it can relay."""
    context.side.record(
        call_id, ToolOutcome(summary="failed", ok=False, error=message)
    )
    return f"The tool could not complete: {message}"


def _readable(exc: Exception) -> str:
    """One sentence about an unexpected failure. Never a traceback.

    A traceback in a tool payload becomes part of a prompt, and from there can
    end up quoted in an answer — so the type and message are all that travel.
    """
    return f"{type(exc).__name__}: {exc}"


def _rag_stages(stages: Sequence[pipeline.StageLog]) -> list[RagStage]:
    """The pipeline's stage log as the chip shows it, renumbered contiguously."""
    shown: list[RagStage] = []
    for stage in stages:
        label = _STAGE_LABELS.get(stage.name)
        if label is None:
            continue
        shown.append(
            RagStage(step=len(shown) + 1, label=label, detail=_stage_detail(stage))
        )
    return shown


def _stage_detail(stage: pipeline.StageLog) -> str:
    """A short, countable description of what one stage did."""
    detail = stage.detail
    if stage.name == "expand":
        queries = detail.get("queries") or []
        text = f"{max(len(queries) - 1, 0)} rewrite(s)"
        return f"{text} · fell back" if detail.get("fallback_reason") else text
    if stage.name == "self_query":
        parts = [
            f"{key}={value}"
            for key, value in (
                ("module", detail.get("module")),
                ("doc_type", detail.get("doc_type")),
            )
            if value
        ]
        sections = detail.get("sections") or []
        if sections:
            parts.append(f"{len(sections)} section(s)")
        if detail.get("dropped"):
            parts.append(f"{len(detail['dropped'])} dropped")
        return " · ".join(parts) or "no filter"
    if stage.name == "retrieve":
        hits = detail.get("hits") or {}
        return f"{detail.get('queries', 0)} query/ies · {len(hits)} ranking(s)"
    if stage.name == "fuse":
        return f"{detail.get('candidates', 0)} candidate(s) · k={detail.get('k')}"
    if stage.name == "rerank":
        text = f"{detail.get('candidates', 0)} → {detail.get('returned', 0)}"
        return f"{text} · fell back" if detail.get("fallback_reason") else text
    return ""


def _candidate_count(stages: Sequence[pipeline.StageLog]) -> int:
    """How many candidates fusion produced — the "of 20" in the chip summary."""
    for stage in stages:
        if stage.name == "fuse":
            return int(stage.detail.get("candidates", 0))
    return 0


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------

#: name -> builder. Story S4.3.2 added the last two, by adding two lines here;
#: that is the entire extension mechanism, and CLAUDE.md caps the abstraction
#: at this dict on purpose. The set must stay equal to ``ToolName`` in
#: ``api/chat_events.py`` (a test asserts it) and to ``TOOL_NAMES`` in
#: ``frontend/lib/events.ts``, whose type guard silently drops a tool it does
#: not know.
TOOL_BUILDERS: dict[str, Callable[[ToolContext], BaseTool]] = {
    "lookup_requirement": _lookup_tool,
    "search_requirements": _search_requirements_tool,
    "search_code": _search_code_tool,
    "check_implementation": _check_implementation_tool,
    "generate_traceability_report": _report_tool,
}


def build_tools(context: ToolContext, *, names: Sequence[str] | None = None) -> list[BaseTool]:
    """Build the registered tools for ``context``, in registry order.

    ``names`` restricts the set — used by tests, and by any future caller that
    wants a narrower agent.
    """
    wanted = list(TOOL_BUILDERS) if names is None else list(names)
    unknown = [name for name in wanted if name not in TOOL_BUILDERS]
    if unknown:
        raise ValueError(
            f"unknown tool(s) {unknown}; registered: {', '.join(TOOL_BUILDERS)}"
        )
    return [TOOL_BUILDERS[name](context) for name in wanted]
