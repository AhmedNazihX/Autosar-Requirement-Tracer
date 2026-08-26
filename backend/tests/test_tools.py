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
from tests.support_engine import PAGE_COUNTS, build_engine, build_index
from tests.support_llm import json_body

NO_FILTER = {"module": None, "doc_type": None, "section": None}


@pytest.fixture
def context(tmp_path: Path):
    """A :class:`ToolContext` over the shared fixture index."""
    parts = build_index(tmp_path)
    yield parts
    parts["conn"].close_all()


def make(parts, *, translate_replies=(), rerank_replies=()) -> tuple[ToolContext, dict]:
    engine, translate_fake, rerank_fake = build_engine(
        parts, translate_replies=translate_replies, rerank_replies=rerank_replies
    )
    ctx = ToolContext.build(engine, side=SideChannel())
    return ctx, {"translate": translate_fake, "rerank": rerank_fake}


def tool_named(ctx: ToolContext, name: str):
    return next(tool for tool in build_tools(ctx) if tool.name == name)


def call(ctx: ToolContext, name: str, call_id: str = "call_1", **args) -> str:
    tool = tool_named(ctx, name)
    return tool.invoke({"args": args, "id": call_id, "name": name, "type": "tool_call"}).content


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------


def test_the_three_retrieval_tools_are_registered(context):
    ctx, _ = make(context)
    assert sorted(tool.name for tool in build_tools(ctx)) == [
        "lookup_requirement",
        "search_code",
        "search_requirements",
    ]


def test_every_registered_tool_is_a_name_the_frontend_knows():
    """A tool outside ``events.ts`` TOOL_NAMES would be dropped by its guard."""
    assert set(TOOL_BUILDERS) <= set(TOOL_NAMES)


def test_the_registry_is_where_wp4_adds_its_two_tools(context):
    """S4.3.2 registers check_implementation and generate_traceability_report."""
    ctx, _ = make(context)
    missing = set(TOOL_NAMES) - set(TOOL_BUILDERS)
    assert missing == {"check_implementation", "generate_traceability_report"}


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
