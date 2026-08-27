"""Tests for the chat SSE wire models and the per-turn cost meter (F3.1).

``frontend/lib/events.ts`` says outright: "The backend's ``POST /chat`` SSE
envelope must match this file... do not fork them." So these tests assert the
*serialised* shape against that contract rather than asserting that Python
objects hold Python values — a field renamed on one side of the proxy and not
the other is exactly the failure a type check on either side alone cannot see.

Two rules come from the frontend's reducer rather than from the plan, and both
are easy to violate without noticing:

* **``usage`` replaces, it does not accumulate** (``event-reducer.ts:120`` is
  ``usage = event.data``). So a turn must emit exactly one ``usage`` event
  carrying the whole turn's cost — one per model call would report only
  whichever happened to arrive last.
* **A stream must end with ``done`` or ``error``.** The reducer reads a stream
  with neither as ``interrupted``, so forgetting the terminal event silently
  mislabels a perfectly good answer.
"""

from __future__ import annotations

import json

import pytest

from api.chat_events import (
    TOOL_NAMES,
    ChatEventEnvelope,
    CodeCitation,
    DoneEvent,
    ErrorEvent,
    RequirementCitation,
    TokenEvent,
    ToolResultEvent,
    ToolStartEvent,
    TurnMeter,
    UpstreamCitation,
    UsageEvent,
    sse_frame,
)
from core.embeddings import EmbeddingUsage
from core.llm import LlmUsage


def payload(event) -> dict:
    """The event as it goes on the wire, parsed back from JSON."""
    return json.loads(ChatEventEnvelope.of(event).model_dump_json())


# --------------------------------------------------------------------------
# the envelope
# --------------------------------------------------------------------------


def test_every_event_serialises_as_a_type_and_data_envelope():
    body = payload(TokenEvent(text="hello"))
    assert sorted(body) == ["data", "type"]
    assert body["type"] == "token"
    assert body["data"] == {"text": "hello"}


@pytest.mark.parametrize(
    ("event", "expected_type"),
    [
        (TokenEvent(text="x"), "token"),
        (
            ToolStartEvent(id="t1", tool="lookup_requirement", args={"req_id": "SWS_Can_00011"}),
            "tool_start",
        ),
        (
            ToolResultEvent(id="t1", status="ok", summary="SWS_Can_00011", duration_ms=12),
            "tool_result",
        ),
        (
            UsageEvent(
                model="m", prompt_tokens=1, completion_tokens=2, cost_usd=0.1, elapsed_ms=3
            ),
            "usage",
        ),
        (DoneEvent(), "done"),
        (ErrorEvent(message="boom"), "error"),
    ],
)
def test_the_type_tag_matches_the_frontends_union(event, expected_type: str):
    assert payload(event)["type"] == expected_type


def test_the_tool_names_are_exactly_the_five_the_frontend_knows():
    """``events.ts`` TOOL_NAMES. A sixth tool would be dropped by its guard."""
    assert TOOL_NAMES == (
        "lookup_requirement",
        "search_requirements",
        "search_code",
        "check_implementation",
        "generate_traceability_report",
    )


def test_an_unknown_tool_name_is_refused():
    with pytest.raises(ValueError):
        ToolStartEvent(id="t1", tool="rm_rf", args={})


# --------------------------------------------------------------------------
# optional fields must be omitted, not sent as null
# --------------------------------------------------------------------------


def test_absent_optional_fields_are_omitted_from_the_wire():
    """``error?`` and ``stages?`` are optional in TS; ``null`` is not the same."""
    body = payload(ToolResultEvent(id="t1", status="ok", summary="5 of 20", duration_ms=40))
    assert body["data"] == {
        "id": "t1",
        "status": "ok",
        "summary": "5 of 20",
        "duration_ms": 40,
    }


def test_a_failed_tool_reports_a_readable_error_not_a_stack_trace():
    body = payload(
        ToolResultEvent(
            id="t1", status="error", summary="failed", duration_ms=5, error="No such requirement."
        )
    )
    assert body["data"]["error"] == "No such requirement."
    assert "Traceback" not in body["data"]["error"]


def test_done_serialises_with_an_empty_data_object():
    """The frontend's guard requires ``data`` to be an object, not null."""
    assert payload(DoneEvent())["data"] == {}


# --------------------------------------------------------------------------
# citations — three kinds, discriminated by `kind`
# --------------------------------------------------------------------------


def test_a_requirement_citation_carries_everything_the_pdf_pane_needs():
    body = payload(
        RequirementCitation(
            req_id="SWS_Can_00011",
            doc="can_driver",
            doc_title="Specification of CAN Driver",
            page=37,
            bbox=(10.0, 20.0, 100.0, 40.0),
            page_count=203,
            section="7.6 L-PDU transmission",
            quote="Can_Write shall...",
        )
    )
    assert body["type"] == "citation"
    assert body["data"]["kind"] == "requirement"
    assert body["data"]["doc"] == "can_driver", "the manifest key, never a filename"
    assert body["data"]["bbox"] == [10.0, 20.0, 100.0, 40.0]


def test_a_page_crossing_requirement_sends_bbox_null_rather_than_omitting_it():
    """``bbox`` is required-but-nullable in TS: the pane says so explicitly."""
    body = payload(
        RequirementCitation(
            req_id="SWS_CAN_91025",
            doc="can_driver",
            doc_title="Specification of CAN Driver",
            page=62,
            bbox=None,
            page_count=203,
        )
    )
    assert body["data"]["bbox"] is None
    assert "bbox" in body["data"]


def test_page_span_is_omitted_because_storage_cannot_populate_it():
    """``events.ts:103`` under ruling R21 — only the start page is stored."""
    body = payload(
        RequirementCitation(
            req_id="SWS_Can_00011",
            doc="can_driver",
            doc_title="Specification of CAN Driver",
            page=37,
            bbox=None,
            page_count=203,
        )
    )
    assert "page_span" not in body["data"]


def test_a_code_citation_names_the_pinned_sha_it_is_true_for():
    body = payload(
        CodeCitation(
            repo_path="communication/CanIf/src/CanIf.c",
            symbol="CanIf_ControllerBusOff",
            line_span=(1450, 1495),
            git_sha="09433770bebb8f27a7b480d7c96d814c68ffed3e",
        )
    )
    assert body["data"]["kind"] == "code"
    assert body["data"]["line_span"] == [1450, 1495]


def test_an_upstream_citation_is_display_only():
    """SRS documents are not ingested, so there is nothing to open."""
    body = payload(
        UpstreamCitation(req_id="SRS_Can_01059", cited_by="SWS_Can_00011", doc="SRS CAN")
    )
    assert body["data"]["kind"] == "upstream"
    assert "page" not in body["data"], "an upstream chip is never clickable through"


# --------------------------------------------------------------------------
# the per-turn cost meter (story S3.1.1)
# --------------------------------------------------------------------------


def test_the_meter_reports_one_usage_event_for_the_whole_turn():
    """The reducer keeps only the last usage event, so there must be one."""
    meter = TurnMeter(model="test/chat-model")
    meter.add_llm(
        LlmUsage(purpose="chat", calls=1, prompt_tokens=100, completion_tokens=40, cost_usd=0.004)
    )
    meter.add_llm(
        LlmUsage(
            purpose="rerank", calls=1, prompt_tokens=900, completion_tokens=20, cost_usd=0.0004
        )
    )
    meter.add_llm(
        LlmUsage(
            purpose="translate", calls=2, prompt_tokens=200, completion_tokens=60, cost_usd=0.0002
        )
    )
    meter.add_embedding(EmbeddingUsage(calls=4, prompt_tokens=40, cost_usd=0.000001))

    event = meter.usage_event(elapsed_ms=1234)

    assert event.model == "test/chat-model", "the chat model names the turn"
    assert event.prompt_tokens == 1240, "every purpose counted"
    assert event.completion_tokens == 120
    assert event.cost_usd == pytest.approx(0.004601)
    assert event.elapsed_ms == 1234


def test_the_meter_keeps_a_per_purpose_breakdown_for_the_stage_log():
    """The single wire figure is a total; debugging needs the split."""
    meter = TurnMeter(model="m")
    meter.add_llm(LlmUsage(purpose="chat", calls=1, cost_usd=0.004))
    meter.add_llm(LlmUsage(purpose="chat", calls=1, cost_usd=0.002))
    meter.add_llm(LlmUsage(purpose="judge", calls=1, cost_usd=0.001))

    assert meter.by_purpose()["chat"].calls == 2
    assert meter.by_purpose()["chat"].cost_usd == pytest.approx(0.006)
    assert meter.by_purpose()["judge"].cost_usd == pytest.approx(0.001)


def test_a_pipeline_result_can_be_added_wholesale():
    """A search reports usage per purpose plus embeddings; none may be dropped."""
    from retrieval.pipeline import PipelineUsage

    meter = TurnMeter(model="m")
    meter.add_pipeline(
        PipelineUsage(
            llm=(
                LlmUsage(purpose="translate", calls=1, cost_usd=0.0001),
                LlmUsage(purpose="rerank", calls=1, cost_usd=0.0004),
            ),
            embedding=EmbeddingUsage(calls=4, cost_usd=0.0000002),
        )
    )

    assert meter.usage_event(elapsed_ms=1).cost_usd == pytest.approx(0.0005002)


def test_an_empty_turn_still_reports_a_usage_event():
    """A turn the agent answered without a tool or a token still costs time."""
    event = TurnMeter(model="m").usage_event(elapsed_ms=42)
    assert event.cost_usd == 0.0
    assert event.elapsed_ms == 42


def test_embedding_usage_is_not_double_counted_as_llm_usage():
    meter = TurnMeter(model="m")
    meter.add_embedding(EmbeddingUsage(calls=1, prompt_tokens=10, cost_usd=0.001))

    assert "embed" not in meter.by_purpose(), "embeddings are reported separately"
    assert meter.usage_event(elapsed_ms=1).cost_usd == pytest.approx(0.001)
    assert meter.usage_event(elapsed_ms=1).prompt_tokens == 10


# --------------------------------------------------------------------------
# SSE framing
# --------------------------------------------------------------------------


def test_a_frame_is_one_data_line_terminated_by_a_blank_line():
    """``chat-sources.ts`` splits on ``\\n\\n`` and reads ``data:`` lines."""
    frame = sse_frame(TokenEvent(text="hello"))

    assert frame.endswith("\n\n")
    assert frame.startswith("data: ")
    assert json.loads(frame[len("data: ") :].strip()) == {
        "type": "token",
        "data": {"text": "hello"},
    }


def test_a_newline_in_the_payload_cannot_break_the_frame():
    """A token containing a blank line would otherwise terminate the frame early."""
    frame = sse_frame(TokenEvent(text="one\n\ntwo"))

    assert frame.count("\n\n") == 1, "only the terminator"
    body = json.loads(frame[len("data: ") :].strip())
    assert body["data"]["text"] == "one\n\ntwo"
