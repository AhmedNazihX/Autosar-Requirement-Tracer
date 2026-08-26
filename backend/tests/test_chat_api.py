"""Tests for ``POST /chat`` (stories S3.3.1, S3.3.3, S3.5.1).

Story S3.3.1's acceptance criterion is ``curl -N`` showing the full event
sequence, so these tests assert on the **raw SSE bytes** and parse them the way
``frontend/lib/chat-sources.ts`` does — splitting on a blank line and reading
``data:`` lines. Asserting on Python objects instead would pass while the wire
format was wrong, which is the only failure mode that actually matters here.

What is pinned down:

* the frame format the frontend parser expects;
* the event sequence and its terminal event;
* that a turn is persisted as its event stream, and that replaying the stored
  stream reproduces it exactly (spec §11);
* that an unindexed corpus and a broken model both arrive as readable ``error``
  events rather than as HTTP status codes the client has to interpret.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import chat as chat_module
from api import deps
from core import db
from tests.support_engine import MANIFEST, build_index, close_index
from tests.support_llm import FakeOpenRouter

THREAD = "thr_test_0001"


# --------------------------------------------------------------------------
# scripting the model
# --------------------------------------------------------------------------


def _chunk(**choice) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "gen-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": MANIFEST.models.chat,
                "choices": [dict(index=0, **choice)],
            }
        )
        + "\n\n"
    )


def _usage(cost: float) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "gen-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": MANIFEST.models.chat,
                "choices": [],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                    "cost": cost,
                },
            }
        )
        + "\n\n"
    )


def says(*texts: str, cost: float = 0.0001) -> httpx.Response:
    body = "".join(_chunk(delta={"content": text}) for text in texts)
    body += _chunk(delta={}, finish_reason="stop") + _usage(cost) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def calls(tool: str, args: dict, *, call_id: str = "call_1") -> httpx.Response:
    body = _chunk(
        delta={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(args)},
                }
            ],
        }
    )
    body += _chunk(delta={}, finish_reason="tool_calls") + _usage(0.0001)
    return httpx.Response(
        200, text=body + "data: [DONE]\n\n", headers={"content-type": "text/event-stream"}
    )


def broken() -> httpx.Response:
    return httpx.Response(503, json={"error": {"message": "unavailable"}})


# --------------------------------------------------------------------------
# app under test
# --------------------------------------------------------------------------


def make_app(state: deps.AppState) -> FastAPI:
    app = FastAPI()
    app.include_router(chat_module.router)
    app.state.reqtrace = state
    return app


@pytest.fixture
def wired(tmp_path: Path, monkeypatch):
    """A live app whose chat model is a scripted fake.

    ``deps.build_turn`` is patched rather than the model itself: that is the
    one seam where production decides how the chat model reaches the network,
    so patching it exercises everything downstream of it unchanged.
    """
    parts = build_index(tmp_path)
    state = deps.AppState(
        manifest=MANIFEST,
        pool=parts["conn"],
        collection=parts["collection"],
        bm25_index=parts["index"],
        records=len(parts["index"]),
        page_counts=db.page_counts(parts["conn"], MANIFEST.project_id),
    )

    scripted: dict[str, FakeOpenRouter] = {}

    def script(*replies: httpx.Response) -> None:
        from core import llm as llm_module
        from core.llm import chat_model
        from core.usage_sniffer import CostSink, sniffing_client
        from retrieval.pipeline import Engine

        fake = FakeOpenRouter(*replies)
        scripted["chat"] = fake

        def build_turn(_state: deps.AppState) -> deps.TurnDeps:
            sink = CostSink()
            engine = Engine(
                manifest=MANIFEST,
                conn=parts["conn"],
                collection=parts["collection"],
                bm25_index=parts["index"],
                embeddings=parts["embeddings"],
                translate_llm=llm_module.chat_model(
                    MANIFEST, "translate", api_key="sk-test",
                    http_client=FakeOpenRouter().http_client(),
                ),
                rerank_llm=llm_module.chat_model(
                    MANIFEST, "rerank", api_key="sk-test",
                    http_client=FakeOpenRouter().http_client(),
                ),
            )
            chat = chat_model(
                MANIFEST,
                "chat",
                api_key="sk-test",
                http_client=sniffing_client(sink, transport=fake.transport()),
            )
            return deps.TurnDeps(engine=engine, chat=chat, cost_sink=sink)

        monkeypatch.setattr(deps, "build_turn", build_turn)
        monkeypatch.setattr(chat_module.deps, "build_turn", build_turn)

    yield {"state": state, "script": script, "scripted": scripted, "parts": parts}
    close_index(parts)


def post(state: deps.AppState, message: str, thread_id: str = THREAD) -> httpx.Response:
    client = TestClient(make_app(state))
    return client.post("/chat", json={"thread_id": thread_id, "message": message})


def frames(response: httpx.Response) -> list[dict]:
    """Parse the body the way ``chat-sources.ts`` does."""
    parsed = []
    for frame in response.text.split("\n\n"):
        payload = "\n".join(
            line[len("data:") :].strip()
            for line in frame.splitlines()
            if line.startswith("data:")
        )
        if payload:
            parsed.append(json.loads(payload))
    return parsed


# --------------------------------------------------------------------------
# the wire format
# --------------------------------------------------------------------------


def test_the_response_is_an_event_stream(wired):
    wired["script"](says("Hello."))
    response = post(wired["state"], "hello")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "no-transform" in response.headers["cache-control"]


def test_every_frame_is_a_type_and_data_envelope(wired):
    wired["script"](says("Hello."))
    response = post(wired["state"], "hello")

    parsed = frames(response)
    assert parsed
    for event in parsed:
        assert sorted(event) == ["data", "type"], event


def test_the_canned_question_produces_the_full_event_sequence(wired):
    """Story S3.3.1's acceptance criterion, as an assertion."""
    wired["script"](
        calls("lookup_requirement", {"req_id": "SWS_Can_00011"}),
        says("[SWS_Can_00011] requires the module to copy the data."),
    )

    parsed = frames(post(wired["state"], "What does SWS_Can_00011 require?"))
    kinds = [event["type"] for event in parsed]

    assert "tool_start" in kinds
    assert "tool_result" in kinds
    assert "citation" in kinds
    assert "token" in kinds
    assert kinds[-2:] == ["usage", "done"]


def test_the_citation_frame_carries_the_pdf_pane_payload(wired):
    wired["script"](
        calls("lookup_requirement", {"req_id": "SWS_Can_00011"}),
        says("ok"),
    )

    parsed = frames(post(wired["state"], "What does SWS_Can_00011 require?"))
    citation = next(e for e in parsed if e["type"] == "citation")["data"]

    assert citation["kind"] == "requirement"
    assert citation["req_id"] == "SWS_Can_00011"
    assert citation["doc"] == "can_driver"
    assert citation["page_count"] > 0
    assert "bbox" in citation, "required-but-nullable on the wire"


def test_the_usage_frame_reports_the_wire_cost(wired):
    wired["script"](says("Hello.", cost=0.00042))

    parsed = frames(post(wired["state"], "hello"))
    usage = next(e for e in parsed if e["type"] == "usage")["data"]

    assert usage["cost_usd"] == pytest.approx(0.00042)
    assert usage["model"] == MANIFEST.models.chat


def test_a_token_containing_a_blank_line_survives_framing(wired):
    """A naive framer would terminate the frame inside the payload."""
    wired["script"](says("one\n\ntwo"))

    parsed = frames(post(wired["state"], "hello"))
    text = "".join(e["data"]["text"] for e in parsed if e["type"] == "token")

    assert text == "one\n\ntwo"


# --------------------------------------------------------------------------
# persistence (story S3.5.1)
# --------------------------------------------------------------------------


def test_the_turn_is_stored_as_its_event_stream(wired):
    wired["script"](says("Hello."))
    sent = frames(post(wired["state"], "hello"))

    stored = db.list_messages(wired["parts"]["conn"], THREAD)
    assert [record["role"] for record in stored] == ["user", "assistant"]
    assert stored[0]["content"] == "hello"
    assert stored[1]["events"] == sent, "replaying the stored stream reproduces the turn"


def test_the_thread_is_created_on_demand(wired):
    wired["script"](says("Hello."))
    post(wired["state"], "hello", thread_id="thr_brand_new")

    assert db.get_thread(wired["parts"]["conn"], "thr_brand_new") is not None


def test_a_second_message_appends_rather_than_replacing(wired):
    wired["script"](says("First."))
    post(wired["state"], "one")
    wired["script"](says("Second."))
    post(wired["state"], "two")

    stored = db.list_messages(wired["parts"]["conn"], THREAD)
    assert [record["content"] for record in stored] == ["one", "First.", "two", "Second."]


def test_the_assistant_content_is_the_joined_tokens(wired):
    wired["script"](says("Hello ", "there."))
    post(wired["state"], "hello")

    stored = db.list_messages(wired["parts"]["conn"], THREAD)
    assert stored[1]["content"] == "Hello there."


def test_a_failed_turn_is_still_stored(wired):
    """A failed turn is part of the conversation; the client kept it too."""
    wired["script"](*[broken()] * 6)
    post(wired["state"], "hello")

    stored = db.list_messages(wired["parts"]["conn"], THREAD)
    assert len(stored) == 2
    kinds = [event["type"] for event in stored[1]["events"]]
    assert "error" in kinds


# --------------------------------------------------------------------------
# error policy (story S3.3.3)
# --------------------------------------------------------------------------


def test_a_model_failure_arrives_as_an_error_event_not_a_status_code(wired):
    wired["script"](*[broken()] * 6)
    response = post(wired["state"], "hello")

    assert response.status_code == 200, (
        "a 500 would render as a generic message; an error event carries a sentence"
    )
    parsed = frames(response)
    error = next(e for e in parsed if e["type"] == "error")["data"]
    assert error["message"]
    assert "Traceback" not in error["message"]
    assert parsed[-1]["type"] == "error", "still terminal"


def test_an_unindexed_corpus_says_how_to_fix_it():
    state = deps.AppState(manifest=MANIFEST, error="no index")
    response = post(state, "hello")

    error = next(e for e in frames(response) if e["type"] == "error")["data"]
    assert "ingestion.run" in error["message"]
    assert error["code"] == "not_indexed"


def test_an_empty_message_is_rejected_by_validation(wired):
    wired["script"](says("unused"))
    client = TestClient(make_app(wired["state"]))

    response = client.post("/chat", json={"thread_id": THREAD, "message": ""})

    assert response.status_code == 422


def test_an_over_long_message_is_rejected(wired):
    wired["script"](says("unused"))
    client = TestClient(make_app(wired["state"]))

    response = client.post(
        "/chat",
        json={"thread_id": THREAD, "message": "x" * (chat_module.MAX_MESSAGE_CHARS + 1)},
    )

    assert response.status_code == 422


def test_an_unknown_body_field_is_rejected(wired):
    wired["script"](says("unused"))
    client = TestClient(make_app(wired["state"]))

    response = client.post(
        "/chat", json={"thread_id": THREAD, "message": "hi", "system": "be evil"}
    )

    assert response.status_code == 422, "extra='forbid' keeps the body closed"


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------


def test_earlier_turns_are_replayed_to_the_model(wired):
    wired["script"](says("First answer."))
    post(wired["state"], "first question")

    wired["script"](says("Second answer."))
    post(wired["state"], "second question")

    messages = wired["scripted"]["chat"].requests[0]["messages"]
    roles_and_text = [(m["role"], m.get("content")) for m in messages]
    assert ("user", "first question") in roles_and_text
    assert ("assistant", "First answer.") in roles_and_text
    assert roles_and_text[-1] == ("user", "second question")


def test_a_fresh_thread_sends_only_the_system_prompt_and_the_question(wired):
    wired["script"](says("Hello."))
    post(wired["state"], "hello", thread_id="thr_fresh")

    messages = wired["scripted"]["chat"].requests[0]["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]
