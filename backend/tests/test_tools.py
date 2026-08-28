"""Tests for the agent's tools and the registry WP4 plugs into (story S3.2.1).

A tool here has two outputs, and both matter:

* **the payload the model reads** — plain text, with every piece of retrieved
  document or code text fenced as data (spec §8). The model is the one thing in
  the system that can be talked into acting, and tool payloads are the channel
  a poisoned document would arrive through.
* **the side channel the SSE stream reads** — citations, RAG stage log, usage,
  and the one-line chip summary. None of that can go through the model: a
  citation must be a structured event, never something parsed back out of
  prose (spec §6).

The rule that keeps the agent alive: **a tool never raises.** A failing tool
returns a payload saying so and records ``ok=False``; the model then explains
the failure instead of the whole turn dying. Every tool is tested for that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools import (
    TOOL_BUILDERS,
    SideChannel,
    ToolContext,
    build_tools,
)
from api.chat_events import TOOL_NAMES, CodeCitation, RequirementCitation, UpstreamCitation
from core.llm import chat_model
from engines.report import ScopeError
from tests.support_engine import MANIFEST, PAGE_COUNTS, build_engine, build_index, close_index
from tests.support_llm import FakeOpenRouter, json_body

NO_FILTER = {"module": None, "doc_type": None, "section": None}


@pytest.fixture
def context(tmp_path: Path):
    """A :class:`ToolContext` over the shared fixture index."""
    parts = build_index(tmp_path)
    yield parts
    close_index(parts)


def make(
    parts,
    *,
    translate_replies=(),
    rerank_replies=(),
    judge_replies=(),
    launch_report=None,
) -> tuple[ToolContext, dict]:
    engine, translate_fake, rerank_fake = build_engine(
        parts, translate_replies=translate_replies, rerank_replies=rerank_replies
    )
    judge_fake = FakeOpenRouter(*judge_replies)
    ctx = ToolContext.build(
        engine,
        side=SideChannel(),
        judge_llm=chat_model(
            MANIFEST, "judge", api_key="sk-test", http_client=judge_fake.http_client()
        ),
        launch_report=launch_report,
    )
    return ctx, {"translate": translate_fake, "rerank": rerank_fake, "judge": judge_fake}


def tool_named(ctx: ToolContext, name: str):
    return next(tool for tool in build_tools(ctx) if tool.name == name)


def call(ctx: ToolContext, name: str, call_id: str = "call_1", **args) -> str:
    tool = tool_named(ctx, name)
    return tool.invoke({"args": args, "id": call_id, "name": name, "type": "tool_call"}).content


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------


def test_all_five_tools_are_registered(context):
    """Three retrieval tools from S3.2.1, two evidence tools from S4.3.2."""
    ctx, _ = make(context)
    assert sorted(tool.name for tool in build_tools(ctx)) == [
        "check_implementation",
        "generate_traceability_report",
        "lookup_requirement",
        "search_code",
        "search_requirements",
    ]


def test_every_registered_tool_is_a_name_the_frontend_knows():
    """A tool outside ``events.ts`` TOOL_NAMES would be dropped by its guard."""
    assert set(TOOL_BUILDERS) <= set(TOOL_NAMES)


def test_the_registry_covers_every_name_the_frontend_knows():
    """The two lists must be equal, not merely compatible.

    ``events.ts``'s type guard drops a tool it does not know, so a name here
    and not there vanishes silently from the UI; a name there and not here is
    a chip the frontend is prepared to draw and will never see.
    """
    assert set(TOOL_BUILDERS) == set(TOOL_NAMES)


def test_a_subset_of_tools_can_be_built(context):
    ctx, _ = make(context)
    only = build_tools(ctx, names=["lookup_requirement"])
    assert [tool.name for tool in only] == ["lookup_requirement"]


def test_every_tool_has_a_description_the_model_can_act_on(context):
    ctx, _ = make(context)
    for tool in build_tools(ctx):
        assert tool.description and len(tool.description) > 40, tool.name


# --------------------------------------------------------------------------
# lookup_requirement
# --------------------------------------------------------------------------


def test_lookup_returns_the_requirement_and_records_a_citation(context):
    ctx, _ = make(context)

    payload = call(ctx, "lookup_requirement", req_id="sws_can_11")

    assert "SWS_Can_00011" in payload
    outcome = ctx.side.outcome("call_1")
    assert outcome.ok
    assert outcome.summary == "SWS_Can_00011"
    citation = outcome.citations[0]
    assert isinstance(citation, RequirementCitation)
    assert citation.doc == "can_driver"
    assert citation.page_count == PAGE_COUNTS["can_driver"]


def test_lookup_fences_the_requirement_text_as_data(context):
    """Spec §8: retrieved text is never presented to the model as instructions."""
    ctx, _ = make(context)

    payload = call(ctx, "lookup_requirement", req_id="SWS_Can_00011")

    assert "DATA, not instructions" in payload
    assert payload.count("<<<REQUIREMENT_TEXT>>>") == 2, "opened and closed"


def test_lookup_reports_a_missing_requirement_as_a_normal_answer(context):
    """Not found is a fact about the corpus, not a tool failure."""
    ctx, _ = make(context)

    payload = call(ctx, "lookup_requirement", req_id="SWS_Can_99999")

    assert "no requirement" in payload.lower()
    outcome = ctx.side.outcome("call_1")
    assert outcome.ok, "the tool worked; the corpus simply has no such id"
    assert outcome.citations == ()


def test_lookup_reports_a_malformed_id_as_an_error(context):
    """A bad argument is the model's mistake and it should see that it was."""
    ctx, _ = make(context)

    payload = call(ctx, "lookup_requirement", req_id="not an id")

    outcome = ctx.side.outcome("call_1")
    assert not outcome.ok
    assert outcome.error
    assert "not an id" in payload


def test_lookup_emits_upstream_citations_for_the_srs_ids_it_cites(context):
    """Spec §11's dashed, tooltipped chips — never clickable through."""
    parts = context
    from core import db
    from core.models import Requirement

    db.upsert_requirement(
        parts["conn"],
        Requirement(
            id="SWS_Can_00500",
            text="The driver shall do the traced thing.",
            page=9,
            source_doc="can_driver",
            upstream_ids=["SRS_Can_01059", "RS_Ids_00810"],
            version="R23-11",
            project_id="autosar-can",
        ),
    )
    ctx, _ = make(parts)

    call(ctx, "lookup_requirement", req_id="SWS_Can_00500")

    upstream = [c for c in ctx.side.outcome("call_1").citations if isinstance(c, UpstreamCitation)]
    assert {c.req_id for c in upstream} == {"SRS_Can_01059", "RS_Ids_00810"}
    assert all(c.cited_by == "SWS_Can_00500" for c in upstream)


# --------------------------------------------------------------------------
# search_requirements
# --------------------------------------------------------------------------


def test_search_requirements_records_citations_stages_and_usage(context):
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off state"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2, 3]})],
    )

    payload = call(ctx, "search_requirements", query="how is bus-off reported?")

    outcome = ctx.side.outcome("call_1")
    assert outcome.ok
    assert outcome.citations, "every result must be citable"
    assert all(isinstance(c, RequirementCitation) for c in outcome.citations)
    assert outcome.stages, "the RAG pipeline must be visible on the chip"
    assert outcome.usage is not None
    assert "SWS_" in payload

    # The funnel fields: the expansion shows its actual rewrites, and every
    # narrowing stage carries the candidate count its bar is drawn from.
    expand = next(s for s in outcome.stages if s.label == "multi-query")
    assert expand.queries, "the rewrites themselves are shown, not just a count"
    assert expand.count == len(expand.queries)
    fused = next(s for s in outcome.stages if s.label == "RRF fusion")
    assert fused.count is not None and fused.count > 0


def test_the_chip_summary_says_how_many_of_how_many(context):
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2]})],
    )

    call(ctx, "search_requirements", query="bus off", top_n=2)

    outcome = ctx.side.outcome("call_1")
    # "returned of fused" — the second number is however many candidates fusion
    # produced, which is corpus-sized, so it is read back rather than hardcoded.
    fused = next(s for s in outcome.stages if s.label == "RRF fusion")
    candidates = int(fused.detail.split()[0])
    assert outcome.summary == f"2 of {candidates}"
    assert candidates >= 2


def test_the_stage_log_names_the_advanced_rag_stages(context):
    """These are what the tool chip shows; the plumbing stage is not one."""
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1]})],
    )

    call(ctx, "search_requirements", query="bus off")

    labels = [stage.label for stage in ctx.side.outcome("call_1").stages]
    assert labels == ["multi-query", "self-query", "hybrid search", "RRF fusion", "LLM rerank"]
    assert "hydrate" not in labels, "loading rows from SQLite is not a retrieval stage"
    steps = [stage.step for stage in ctx.side.outcome("call_1").stages]
    assert steps == [1, 2, 3, 4, 5], "1-based and contiguous"


def test_search_requirements_fences_every_result(context):
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2]})],
    )

    payload = call(ctx, "search_requirements", query="bus off", top_n=2)

    assert payload.count("<<<REQUIREMENT_TEXT>>>") == 4, "two results, opened and closed"
    assert "DATA, not instructions" in payload


def test_explicit_filters_reach_the_pipeline(context):
    """The tool signature is ``search_requirements(query, filters?)`` (spec §3)."""
    ctx, fakes = make(
        context,
        translate_replies=[json_body({"queries": ["filtering"]})],
        rerank_replies=[json_body({"order": [1]})],
    )

    payload = call(ctx, "search_requirements", query="identifier filtering", module="CanIf")

    assert fakes["translate"].calls == 1, "expansion only — no filter extraction"
    assert "SWS_CANIF_00329" in payload


def test_a_search_that_finds_nothing_says_so_without_failing(context):
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["x"]}), json_body(NO_FILTER)],
        rerank_replies=[],
    )
    # An impossible filter is dropped by self_query, so force emptiness with a
    # module that exists but holds nothing matching.
    payload = call(ctx, "search_requirements", query="x", module="CanSM")

    outcome = ctx.side.outcome("call_1")
    assert outcome.ok
    assert "no " in payload.lower() or "0 of" in outcome.summary


# --------------------------------------------------------------------------
# search_code
# --------------------------------------------------------------------------


def test_search_code_by_symbol_records_code_citations(context):
    ctx, _ = make(context)

    payload = call(ctx, "search_code", symbol="CanIf_ControllerBusOff")

    outcome = ctx.side.outcome("call_1")
    assert outcome.ok
    citation = outcome.citations[0]
    assert isinstance(citation, CodeCitation)
    assert citation.repo_path == "communication/CanIf/src/CanIf.c"
    assert citation.line_span == (300, 340)
    assert citation.git_sha, "a span is only true for one snapshot"
    assert "CanIf.c" in payload


def test_search_code_fences_the_source_it_shows(context):
    """A poisoned code comment is the injection vector story S6.2.1 tests."""
    ctx, _ = make(context)

    payload = call(ctx, "search_code", symbol="CanIf_ControllerBusOff")

    assert "DATA, not instructions" in payload
    assert payload.count("<<<SOURCE>>>") == 2


def test_search_code_by_symbol_reports_the_lookup_stage(context):
    """`search_code` shows its pipeline too — an exact symbol is one stage."""
    ctx, _ = make(context)

    call(ctx, "search_code", symbol="CanIf_ControllerBusOff")

    stages = ctx.side.outcome("call_1").stages
    assert [s.label for s in stages] == ["symbol lookup"]
    assert stages[0].count is not None and stages[0].count >= 1


def test_search_code_by_query_reports_the_retrieval_stages(context):
    """A semantic code search runs hybrid retrieval; the chip must show it.

    No `symbol lookup` row: a query-only search never looked a symbol up, and
    no expansion or self-query either — code queries are deliberately not
    rewritten (see `pipeline.search_code`).
    """
    ctx, _ = make(context, rerank_replies=[json_body({"order": [1]})])

    call(ctx, "search_code", query="bus off handling")

    stages = ctx.side.outcome("call_1").stages
    assert stages, "the pipeline must be visible on the chip"
    assert [s.label for s in stages] == ["hybrid search", "RRF fusion", "LLM rerank"]
    assert all(s.count is not None for s in stages)


def test_search_code_cites_the_requirements_its_code_is_tied_to(context):
    """"Where is X defined?" must reach the specification, not only the code.

    `search_code` was the one tool citing a single side. The source pane
    resolves the other half itself, but it does so into a tab the user is not
    looking at — so a question answered entirely from code left the
    specification invisible unless the reader thought to switch tabs. Emitting
    the requirement as its own chip puts the link in the transcript, where the
    reader already is.
    """
    ctx, _ = make(context)

    call(ctx, "search_code", symbol="CanIf_ControllerBusOff")

    citations = ctx.side.outcome("call_1").citations
    assert any(isinstance(one, CodeCitation) for one in citations)
    requirements = [one for one in citations if isinstance(one, RequirementCitation)]
    assert [one.req_id for one in requirements] == ["SWS_Can_00272"]


def test_search_code_puts_its_code_citation_first(context):
    """The pane opens on the first citation, and the code is the answer here.

    `targetForEvents` (frontend/lib/source-target.ts) takes the first citation
    of a turn, so this ordering is what keeps "where is X defined?" opening on
    the Code tab rather than jumping to a PDF page.
    """
    ctx, _ = make(context)

    call(ctx, "search_code", symbol="CanIf_ControllerBusOff")

    citations = ctx.side.outcome("call_1").citations
    assert isinstance(citations[0], CodeCitation)


def test_search_code_never_cites_a_requirement_the_code_denies(context):
    """A `!req` says the developers believe this code does NOT implement it.

    Citing it under an answer about what the code *is* would invert its
    meaning — the same rule `bestRequirementLink` applies in the pane.
    """
    ctx, _ = make(context)

    call(ctx, "search_code", symbol="CanIf_Transmit")

    cited = {
        one.req_id
        for one in ctx.side.outcome("call_1").citations
        if isinstance(one, RequirementCitation)
    }
    assert "SWS_CANIF_00023" in cited, "the claimed requirement should be cited"
    assert "SWS_CANIF_00329" not in cited, "a !req was cited as evidence"


def test_search_code_reports_the_line_span_the_pane_scrolls_to(context):
    ctx, _ = make(context)
    payload = call(ctx, "search_code", symbol="CanIf_ControllerBusOff")
    assert "300-340" in payload


def test_search_code_needs_a_query_or_a_symbol(context):
    ctx, _ = make(context)

    payload = call(ctx, "search_code")

    outcome = ctx.side.outcome("call_1")
    assert not outcome.ok
    assert "query" in payload.lower()


# --------------------------------------------------------------------------
# a tool never raises
# --------------------------------------------------------------------------


def test_a_tool_that_breaks_returns_an_error_instead_of_raising(context, tmp_path):
    """The turn must survive: the model explains the failure to the user.

    Broken by pointing the pool at an unopenable path rather than by closing a
    connection — the pool reopens on demand, which is the whole point of it.
    """
    import dataclasses

    from core import db

    ctx, _ = make(context)
    unopenable = db.pooled(tmp_path / "no-such-dir" / "broken.db")
    ctx = dataclasses.replace(ctx, engine=dataclasses.replace(ctx.engine, conn=unopenable))

    payload = call(ctx, "lookup_requirement", req_id="SWS_Can_00011")

    outcome = ctx.side.outcome("call_1")
    assert not outcome.ok
    assert outcome.error
    assert "Traceback" not in payload, "the model sees a sentence, not a stack trace"


def test_two_calls_in_one_turn_are_recorded_separately(context):
    """Chips are correlated by tool_call_id; sharing state would merge them."""
    ctx, _ = make(context)

    call(ctx, "lookup_requirement", call_id="a", req_id="SWS_Can_00011")
    call(ctx, "lookup_requirement", call_id="b", req_id="SWS_Can_00020")

    assert ctx.side.outcome("a").summary == "SWS_Can_00011"
    assert ctx.side.outcome("b").summary == "SWS_Can_00020"


def test_an_unrecorded_call_id_has_no_outcome(context):
    ctx, _ = make(context)
    assert ctx.side.outcome("never-called") is None


def test_only_requirements_become_citation_chips(context):
    """Context prose informs the answer but is not a citable requirement.

    Its id is synthetic (``CTX_can_driver_6_14``) and the frontend renders a
    chip as ``[{req_id}]``, so citing prose shows the user a meaningless
    label. Found on the real corpus: two of five chips for a bus-off question
    were CTX ids.
    """
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2, 3, 4, 5]})],
    )

    payload = call(ctx, "search_requirements", query="how is bus-off reported?", top_n=5)

    cited = {c.req_id for c in ctx.side.outcome("call_1").citations}
    assert cited, "requirements must still be cited"
    assert not any(req_id.startswith("CTX_") for req_id in cited)
    # The prose is still shown to the model, which is the point of keeping it.
    assert "CTX_can_driver" in payload or len(cited) == 5


# --------------------------------------------------------------------------
# check_implementation (story S4.3.2)
# --------------------------------------------------------------------------


def verdict_reply(status="implemented", *, evidence_items=(), confidence=0.82):
    return json_body(
        {
            "status": status,
            "evidence": [{"candidate": n, "rationale": why} for n, why in evidence_items],
            "confidence": confidence,
            "rationale": f"scripted {status}.",
        }
    )


class _Launched:
    """What ``api.reports.launch_in_thread`` returns, minus FastAPI."""

    def __init__(self, **fields):
        self.__dict__.update(
            {
                "job_id": "job_1",
                "scope_label": "module CanIf",
                "requirements": 2,
                "to_judge": 2,
                "cached": 0,
                "est_cost_usd": 0.0412,
                "est_basis": "openrouter catalogue prices",
                "ceiling_usd": 2.0,
                "unknown_ids": [],
                **fields,
            }
        )


def test_check_implementation_reports_the_verdict_and_its_evidence(context):
    ctx, _ = make(
        context,
        judge_replies=[verdict_reply("implemented", evidence_items=[(1, "it schedules it.")])],
    )

    payload = call(ctx, "check_implementation", req_id="SWS_CANIF_00023")

    assert "implemented" in payload
    assert "communication/CanIf/src/CanIf.c:120-168" in payload
    assert "it schedules it." in payload


def test_check_implementation_cites_the_requirement_and_the_code(context):
    """Spec §6: the source pane opens from structured events, never from prose."""
    ctx, _ = make(
        context,
        judge_replies=[verdict_reply("partial", evidence_items=[(1, "half of it.")])],
    )

    call(ctx, "check_implementation", req_id="SWS_CANIF_00023")

    citations = ctx.side.outcome("call_1").citations
    assert isinstance(citations[0], RequirementCitation)
    assert citations[0].req_id == "SWS_CANIF_00023"
    code = [c for c in citations if isinstance(c, CodeCitation)]
    assert [(c.repo_path, c.line_span) for c in code] == [
        ("communication/CanIf/src/CanIf.c", (120, 168))
    ]


def test_check_implementation_summarises_the_verdict_on_the_chip(context):
    ctx, _ = make(context, judge_replies=[verdict_reply("missing")])

    call(ctx, "check_implementation", req_id="SWS_CANIF_00023")

    assert ctx.side.outcome("call_1").summary == "SWS_CANIF_00023: missing"


def test_check_implementation_reports_its_evidence_stages(context):
    """The chip shows HOW the verdict's candidates were found — the free tiers
    by name, then whatever the semantic pass ran, then the judge itself."""
    ctx, _ = make(
        context,
        judge_replies=[verdict_reply("implemented", evidence_items=[(1, "it does.")])],
    )

    call(ctx, "check_implementation", req_id="SWS_CANIF_00023")

    stages = ctx.side.outcome("call_1").stages
    assert stages, "the evidence pipeline must be visible on the chip"
    labels = [s.label for s in stages]
    assert labels[0] == "annotation scan"
    assert labels[1] == "symbol anchors"
    assert labels[-1] == "LLM judge"
    assert "hydrate" not in labels, "plumbing is not a stage here either"
    judge = stages[-1]
    assert judge.count is not None
    assert "implemented" in judge.detail


def test_check_implementation_frames_a_missing_verdict_as_release_drift(context):
    """Finding A1: most of the CAN Driver has no implementation here, and a
    tool that implied a defect would teach the model to say so 239 times."""
    ctx, _ = make(context, judge_replies=[verdict_reply("missing")])

    payload = call(ctx, "check_implementation", req_id="SWS_CANIF_00023")

    assert "release drift" in payload
    assert "not as a defect" in payload


def test_check_implementation_survives_an_unknown_id(context):
    ctx, _ = make(context, judge_replies=[])

    payload = call(ctx, "check_implementation", req_id="SWS_Can_99999")

    assert "unverifiable" in payload
    assert ctx.side.outcome("call_1").ok


def test_check_implementation_says_so_when_no_judge_is_configured(context):
    engine, _, _ = build_engine(context)
    ctx = ToolContext.build(engine, side=SideChannel())

    payload = call(ctx, "check_implementation", req_id="SWS_CANIF_00023")

    assert "could not complete" in payload
    assert not ctx.side.outcome("call_1").ok


# --------------------------------------------------------------------------
# generate_traceability_report (story S4.3.2)
# --------------------------------------------------------------------------


def test_the_report_tool_launches_a_job_and_reports_the_estimate(context):
    seen = []
    ctx, _ = make(
        context,
        launch_report=lambda scope: (seen.append(scope), _Launched())[1],
    )

    payload = call(ctx, "generate_traceability_report", module="CanIf")

    assert seen[0].module == "CanIf"
    assert "job_1" in payload
    assert "$0.0412" in payload
    assert "$2.00 ceiling" in payload


def test_the_report_tool_tells_the_model_the_job_is_not_finished(context):
    """The tool returns in milliseconds and the run takes minutes. Without
    this the model narrates a matrix that does not exist yet."""
    ctx, _ = make(context, launch_report=lambda scope: _Launched())

    payload = call(ctx, "generate_traceability_report", module="CanIf")

    assert "not finished yet" in payload
    assert "do not describe its results" in payload


def test_the_report_tool_passes_every_scope_option_through(context):
    seen = []
    ctx, _ = make(
        context, launch_report=lambda scope: (seen.append(scope), _Launched())[1]
    )

    call(
        ctx,
        "generate_traceability_report",
        req_ids=["SWS_Can_00011"],
        rejudge=True,
        limit=5,
    )

    scope = seen[0]
    assert (scope.req_ids, scope.rejudge, scope.limit) == (["SWS_Can_00011"], True, 5)


def test_the_report_tool_records_the_job_id_for_the_drawer(context):
    """The frontend's report drawer attaches to the launched job by id, and
    prose is never parsed (spec §6) — so the id must ride the side channel."""
    ctx, _ = make(context, launch_report=lambda scope: _Launched())

    call(ctx, "generate_traceability_report", module="CanIf")

    assert ctx.side.outcome("call_1").job_id == "job_1"


def test_the_report_tool_reports_an_unknown_scope_as_a_failure(context):
    def boom(scope):
        raise ScopeError("no module 'Ethernet' in this corpus (known: Can, CanIf)")

    ctx, _ = make(context, launch_report=boom)

    payload = call(ctx, "generate_traceability_report", module="Ethernet")

    assert "Ethernet" in payload
    assert not ctx.side.outcome("call_1").ok


def test_the_report_tool_reports_an_unavailable_estimate_honestly(context):
    ctx, _ = make(
        context,
        launch_report=lambda scope: _Launched(
            est_cost_usd=None, est_basis="estimate unavailable — ConnectError"
        ),
    )

    payload = call(ctx, "generate_traceability_report", module="CanIf")

    assert "unknown amount" in payload
    assert "ConnectError" in payload


def test_the_report_tool_mentions_ids_that_are_not_in_the_corpus(context):
    ctx, _ = make(
        context,
        launch_report=lambda scope: _Launched(unknown_ids=["SWS_Can_99999"]),
    )

    payload = call(ctx, "generate_traceability_report", req_ids=["SWS_Can_99999"])

    assert "SWS_Can_99999" in payload


def test_the_report_tool_says_so_when_it_cannot_launch(context):
    ctx, _ = make(context)  # no launcher wired

    payload = call(ctx, "generate_traceability_report", module="CanIf")

    assert "could not complete" in payload
    assert not ctx.side.outcome("call_1").ok


# --------------------------------------------------------------------------
# requirements vs context in the payload
#
# Measured defect: asked "which requirements cover CanIf initialisation", the
# agent answered with two `CTX_*` ids presented as bulleted requirements. They
# carry no citation chip and resolve to no page, so they were dead references
# in the middle of the answer — and the cause was that the payload rendered a
# context passage and a requirement identically.
# --------------------------------------------------------------------------


def test_context_passages_are_labelled_apart_from_requirements(context):
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2, 3, 4, 5]})],
    )

    payload = call(ctx, "search_requirements", query="how is bus-off reported?")

    assert "REQUIREMENT [SWS_" in payload
    if "CTX_" in payload:
        assert "CONTEXT (not a requirement)" in payload
        assert "never cite it as a requirement and never print its id" in payload


def test_the_payload_counts_requirements_and_passages_separately(context):
    """The model needs to know how many actual requirements it has, not how
    many results — those are different numbers whenever context is retrieved."""
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2, 3]})],
    )

    payload = call(ctx, "search_requirements", query="how is bus-off reported?")

    assert "normative requirement(s) and" in payload
    assert "background passage(s), best first." in payload


def test_a_saturated_result_set_says_the_list_may_be_incomplete(context):
    """"Which requirements cover X" is an enumeration: returning the top N
    without saying it is a top N answers a different question."""
    ctx, _ = make(
        context,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1]})],
    )

    payload = call(ctx, "search_requirements", query="bus off", top_n=1)

    assert "not the complete set" in payload
    assert "larger top_n" in payload


def test_the_tool_description_tells_the_model_when_to_widen_the_search(context):
    """`top_n` existed all along and nothing pointed the model at it, so an
    enumeration question silently got the five-result default."""
    ctx, _ = make(context)
    tool = tool_named(ctx, "search_requirements")

    assert "top_n" in tool.description
    assert "enumeration" in tool.description
