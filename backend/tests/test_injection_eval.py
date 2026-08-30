"""Tests for the injection eval's chat delivery (feature F6.2).

The chat probes go through the real ``POST /chat`` endpoint so every attack
leaves a persistent thread that can be opened in the sidebar afterwards. What
would quietly corrupt that measurement is the plumbing: the SSE body must be
read the way ``chat-sources.ts`` reads it, a failed stream must fail the row
rather than score an empty answer as "held", and every chat fixture must get
its own fresh thread. The HTTP layer is injected (``post``), so these tests
never open a socket.
"""

from __future__ import annotations

import json

import pytest

from api.chat_events import (
    DoneEvent,
    ErrorEvent,
    RequirementCitation,
    TokenEvent,
    UsageEvent,
    sse_frame,
)
from core.llm import chat_model
from evaluation import injection_eval
from tests.support_engine import MANIFEST, build_engine, build_index, close_index
from tests.support_llm import FakeOpenRouter


@pytest.fixture
def parts(tmp_path):
    built = build_index(tmp_path)
    yield built
    close_index(built)


def usage(cost_usd: float) -> UsageEvent:
    return UsageEvent(
        model="scripted", prompt_tokens=10, completion_tokens=5,
        cost_usd=cost_usd, elapsed_ms=100,
    )


def citation(req_id: str) -> RequirementCitation:
    return RequirementCitation(
        req_id=req_id, doc="can_if", doc_title="CAN Interface",
        page=1, bbox=None, page_count=100,
    )


def body_of(*events) -> str:
    return "".join(sse_frame(event) for event in events)


class RecordingPost:
    """A stand-in for the HTTP layer: records calls, replays scripted bodies."""

    def __init__(self, *replies: tuple[int, str]):
        self.replies = list(replies)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict) -> tuple[int, str]:
        self.calls.append((url, payload))
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def load_fixture(fixture_id: str) -> dict:
    path = injection_eval.DEFAULT_SET_DIR / f"{fixture_id.replace('-', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# the chat probe over HTTP
# --------------------------------------------------------------------------


def test_chat_probe_reads_answer_citations_and_cost_from_the_stream():
    fixture = load_fixture("citation-forging")
    post = RecordingPost((200, body_of(
        TokenEvent(text="There is no such "),
        TokenEvent(text="requirement."),
        citation("SWS_CANIF_00023"),
        usage(0.0123),
        DoneEvent(),
    )))

    answer, cited, spent = injection_eval.chat_probe(
        post, "http://localhost:8000", fixture, thread_id="injection-abc-citation-forging"
    )

    assert answer == "There is no such requirement."
    assert cited == {"SWS_CANIF_00023"}
    assert spent == pytest.approx(0.0123)
    url, payload = post.calls[0]
    assert url == "http://localhost:8000/chat"
    assert payload == {
        "thread_id": "injection-abc-citation-forging",
        "message": fixture["live"]["question"],
    }


def test_chat_probe_fails_the_row_when_the_stream_ends_in_error():
    fixture = load_fixture("prompt-extraction")
    post = RecordingPost((200, body_of(
        TokenEvent(text="partial"),
        ErrorEvent(message="the model went away", code="stream_died"),
    )))

    with pytest.raises(RuntimeError, match="the model went away"):
        injection_eval.chat_probe(post, "http://localhost:8000", fixture, thread_id="t")


def test_chat_probe_fails_the_row_on_a_non_200_status():
    fixture = load_fixture("prompt-extraction")
    post = RecordingPost((429, '{"detail": "Too many questions at once."}'))

    with pytest.raises(RuntimeError, match="429"):
        injection_eval.chat_probe(post, "http://localhost:8000", fixture, thread_id="t")


# --------------------------------------------------------------------------
# the run leaves a persistent thread per chat fixture
# --------------------------------------------------------------------------


def test_run_gives_every_chat_fixture_its_own_thread_and_records_it(parts):
    engine, _, _ = build_engine(parts)
    fixtures = [load_fixture("prompt-extraction"), load_fixture("citation-forging")]
    post = RecordingPost((200, body_of(
        TokenEvent(text="I answer only from the indexed corpus."),
        usage(0.001),
        DoneEvent(),
    )))
    silent = chat_model(
        MANIFEST, "judge", api_key="sk-test", http_client=FakeOpenRouter().http_client()
    )

    result = injection_eval.run(
        engine,
        fixtures,
        judge_llm=silent,
        grader=silent,
        api="http://localhost:8000",
        post=post,
        run_id="deadbeef",
    )

    assert [row.thread_id for row in result.rows] == [
        "injection-deadbeef-prompt-extraction",
        "injection-deadbeef-citation-forging",
    ]
    assert [payload["thread_id"] for _, payload in post.calls] == [
        "injection-deadbeef-prompt-extraction",
        "injection-deadbeef-citation-forging",
    ]
    assert result.chat_model_id == MANIFEST.models.chat
    assert {row.outcome for row in result.rows} == {injection_eval.HELD}


def test_the_thread_id_survives_the_saved_run_round_trip():
    row = injection_eval.Row(
        id="prompt-extraction", kind="prompt_extraction", probe="chat",
        scoring="prompt_leak", outcome=injection_eval.HELD, detail="held",
        thread_id="injection-deadbeef-prompt-extraction",
    )
    result = injection_eval.RunResult(
        chat_model_id="chat", judge_model_id="judge", grader_model_id="judge",
        git_sha="a" * 40, corpus_version="1", rows=[row],
    )

    rebuilt = injection_eval.RunResult.from_dict(result.as_dict())

    assert rebuilt.rows[0].thread_id == "injection-deadbeef-prompt-extraction"


def test_a_saved_run_without_thread_ids_still_loads():
    """The 2026-08-28 run at docs/evaluations/ predates the field."""
    old = {
        "id": "x", "kind": "k", "probe": "chat", "scoring": "prompt_leak",
        "outcome": injection_eval.HELD, "detail": "held",
    }
    assert injection_eval.Row(**old).thread_id is None
