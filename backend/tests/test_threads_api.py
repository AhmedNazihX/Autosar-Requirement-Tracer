"""Tests for thread CRUD and auto-titling (stories S3.5.2, S3.5.3).

The acceptance criterion is "reload returns an identical event stream; title
generated", so the load-bearing test here is the round trip: what
``GET /threads/{id}`` returns must be byte-identical to what ``POST /chat``
streamed, because the frontend rebuilds the entire render from it (spec §11).

Auto-titling has a second, quieter requirement that this file also guards: it
must not reach the network unless a model is explicitly handed to it. An earlier
version built its own model at the call site, which made every chat test in the
suite call OpenRouter for real — the suite got 50% slower and nothing failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import chat as chat_module
from api import deps, threads
from core import db
from core.llm import chat_model
from tests.support_engine import MANIFEST, build_index
from tests.support_llm import FakeOpenRouter, openrouter_body

PROJECT = MANIFEST.project_id


@pytest.fixture
def wired(tmp_path: Path):
    parts = build_index(tmp_path)
    state = deps.AppState(
        manifest=MANIFEST,
        pool=parts["conn"],
        collection=parts["collection"],
        bm25_index=parts["index"],
        records=len(parts["index"]),
        page_counts=db.page_counts(parts["conn"], PROJECT),
    )
    app = FastAPI()
    app.include_router(threads.router)
    app.include_router(chat_module.router)
    app.state.reqtrace = state
    yield TestClient(app), state, parts
    parts["conn"].close_all()


def titler(*replies) -> object:
    fake = FakeOpenRouter(*replies)
    return chat_model(MANIFEST, "title", api_key="sk-test", http_client=fake.http_client())


# --------------------------------------------------------------------------
# CRUD
# --------------------------------------------------------------------------


def test_a_new_thread_starts_empty_and_untitled(wired):
    http, _, _ = wired
    body = http.post("/threads", json={}).json()

    assert body["title"] == threads.UNTITLED
    assert body["messages"] == []
    assert body["id"]


def test_a_client_supplied_id_is_honoured(wired):
    """The frontend mints ids so it can open a thread without a round trip."""
    http, _, _ = wired
    body = http.post("/threads", json={"id": "thr_mine"}).json()
    assert body["id"] == "thr_mine"


def test_creating_the_same_id_twice_does_not_fail(wired):
    http, _, _ = wired
    assert http.post("/threads", json={"id": "thr_mine"}).status_code == 201
    assert http.post("/threads", json={"id": "thr_mine"}).status_code == 201


def test_threads_are_listed_most_recently_updated_first(wired):
    http, _, parts = wired
    http.post("/threads", json={"id": "thr_old", "title": "Old"})
    http.post("/threads", json={"id": "thr_new", "title": "New"})
    db.create_message(parts["conn"], "thr_old", role="user", content="bump", events=[])

    listed = http.get("/threads").json()

    assert [row["id"] for row in listed][0] == "thr_old", "bumped by a new message"


def test_a_summary_counts_its_messages(wired):
    http, _, parts = wired
    http.post("/threads", json={"id": "thr_counted"})
    for n in range(3):
        db.create_message(parts["conn"], "thr_counted", role="user", content=f"{n}", events=[])

    row = next(r for r in http.get("/threads").json() if r["id"] == "thr_counted")
    assert row["message_count"] == 3


def test_renaming_a_thread_returns_the_new_title(wired):
    http, _, _ = wired
    http.post("/threads", json={"id": "thr_r"})

    body = http.patch("/threads/thr_r", json={"title": "Bus-off handling"}).json()

    assert body["title"] == "Bus-off handling"
    assert http.get("/threads/thr_r").json()["title"] == "Bus-off handling"


def test_an_over_long_title_is_truncated_not_rejected(wired):
    http, _, _ = wired
    http.post("/threads", json={"id": "thr_r"})

    body = http.patch("/threads/thr_r", json={"title": "x" * 120}).json()

    assert len(body["title"]) == threads.MAX_TITLE_CHARS


def test_deleting_a_thread_removes_its_messages(wired):
    http, _, parts = wired
    http.post("/threads", json={"id": "thr_d"})
    db.create_message(parts["conn"], "thr_d", role="user", content="hi", events=[])

    assert http.delete("/threads/thr_d").status_code == 204
    assert http.get("/threads/thr_d").status_code == 404
    assert db.list_messages(parts["conn"], "thr_d") == [], "cascade, not orphans"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/threads/nope", None),
        ("patch", "/threads/nope", {"title": "x"}),
        ("delete", "/threads/nope", None),
    ],
)
def test_an_unknown_thread_is_a_clean_404(wired, method: str, path: str, body):
    http, _, _ = wired
    response = getattr(http, method)(path, **({"json": body} if body else {}))

    assert response.status_code == 404
    assert "No thread" in response.json()["detail"]


def test_an_unknown_field_is_rejected(wired):
    http, _, _ = wired
    assert http.post("/threads", json={"colour": "red"}).status_code == 422


# --------------------------------------------------------------------------
# the round trip that makes reload an exact replay (spec §11)
# --------------------------------------------------------------------------


def test_a_reloaded_thread_returns_the_exact_event_stream_that_was_streamed(
    wired, monkeypatch
):
    """The acceptance criterion for story S3.5.1, end to end."""
    http, state, parts = wired

    def _chunk(**choice):
        return (
            "data: "
            + json.dumps(
                {
                    "id": "g",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": MANIFEST.models.chat,
                    "choices": [dict(index=0, **choice)],
                }
            )
            + "\n\n"
        )

    reply = httpx.Response(
        200,
        text=(
            _chunk(delta={"content": "Hello "})
            + _chunk(delta={"content": "there."})
            + _chunk(delta={}, finish_reason="stop")
            + "data: [DONE]\n\n"
        ),
        headers={"content-type": "text/event-stream"},
    )
    fake = FakeOpenRouter(reply)

    from core.usage_sniffer import CostSink, sniffing_client
    from retrieval.pipeline import Engine

    def build_turn(_state):
        sink = CostSink()
        return deps.TurnDeps(
            engine=Engine(
                manifest=MANIFEST,
                conn=parts["conn"],
                collection=parts["collection"],
                bm25_index=parts["index"],
                embeddings=parts["embeddings"],
                translate_llm=chat_model(
                    MANIFEST, "translate", api_key="sk-test",
                    http_client=FakeOpenRouter().http_client(),
                ),
                rerank_llm=chat_model(
                    MANIFEST, "rerank", api_key="sk-test",
                    http_client=FakeOpenRouter().http_client(),
                ),
            ),
            chat=chat_model(
                MANIFEST, "chat", api_key="sk-test",
                http_client=sniffing_client(sink, transport=fake.transport()),
            ),
            cost_sink=sink,
            titler=None,
        )

    monkeypatch.setattr(chat_module.deps, "build_turn", build_turn)

    response = http.post("/chat", json={"thread_id": "thr_rt", "message": "hello"})
    streamed = [
        json.loads("\n".join(
            line[5:].strip() for line in frame.splitlines() if line.startswith("data:")
        ))
        for frame in response.text.split("\n\n")
        if any(line.startswith("data:") for line in frame.splitlines())
    ]

    reloaded = http.get("/threads/thr_rt").json()
    assistant = reloaded["messages"][1]

    assert assistant["events"] == streamed, "reload must replay byte-identically"
    assert assistant["content"] == "Hello there."
    assert reloaded["messages"][0]["content"] == "hello"


# --------------------------------------------------------------------------
# auto-titling (story S3.5.3)
# --------------------------------------------------------------------------


def test_a_title_is_generated_from_the_first_message(wired):
    http, state, _ = wired
    http.post("/threads", json={"id": "thr_t"})

    title = threads.autotitle(
        state, "thr_t", "What does SWS_Can_00011 require?",
        titler(openrouter_body("Can_Write buffer copying requirement")),
    )

    assert title == "Can_Write buffer copying requirement"
    assert http.get("/threads/thr_t").json()["title"] == title


def test_a_model_that_dresses_up_the_title_is_cleaned(wired):
    http, state, _ = wired
    http.post("/threads", json={"id": "thr_t"})

    title = threads.autotitle(
        state, "thr_t", "q", titler(openrouter_body('  "Bus-off handling."  \n'))
    )

    assert title == "Bus-off handling"


def test_an_already_titled_thread_is_never_retitled(wired):
    """A user's rename must survive the next exchange."""
    http, state, _ = wired
    http.post("/threads", json={"id": "thr_t", "title": "My name"})

    assert threads.autotitle(state, "thr_t", "q", titler(openrouter_body("Other"))) is None
    assert http.get("/threads/thr_t").json()["title"] == "My name"


def test_a_failing_titler_leaves_the_thread_untitled(wired):
    http, state, _ = wired
    http.post("/threads", json={"id": "thr_t"})

    result = threads.autotitle(
        state, "thr_t", "q", titler(httpx.Response(503, json={"error": {"message": "x"}}))
    )

    assert result is None
    assert http.get("/threads/thr_t").json()["title"] == threads.UNTITLED


def test_no_titler_means_no_network_call_and_no_title(wired):
    """The regression guard: autotitle must never build its own model.

    It used to, which made every chat test in the suite call OpenRouter for
    real. Nothing failed — the suite just got 50% slower.
    """
    http, state, _ = wired
    http.post("/threads", json={"id": "thr_t"})

    assert threads.autotitle(state, "thr_t", "q", None) is None
    assert http.get("/threads/thr_t").json()["title"] == threads.UNTITLED


def test_titling_an_unknown_thread_is_a_no_op(wired):
    _, state, _ = wired
    assert threads.autotitle(state, "thr_missing", "q", titler()) is None
