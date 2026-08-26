"""Tests for reading OpenRouter's real cost off the wire (finding D1).

Measured while building the agent: **langchain-openai discards OpenRouter's
``usage.cost`` on the streaming path.** Token counts reach
``AIMessageChunk.usage_metadata``, but the provider's raw usage dict — the only
place the actual charge appears — is dropped, and ``stream_usage=True`` does not
bring it back. Non-streaming calls keep it in
``response_metadata["token_usage"]["cost"]``; streaming ones do not.

That matters because the chat model is streamed (the product *is* the streaming
answer) and is the largest cost in a turn. Estimating it from a price table is
exactly what finding D1 forbids. So the cost is read from the HTTP response
itself, which we can do because this project already injects its own
``http_client``.

The one subtlety, and the reason this needs a test rather than a comment: the
SDK stops iterating the response body at ``[DONE]``, abandoning the generator.
Anything that scans *after* the loop never runs — the first version of this
sniffed nothing while every other assertion passed.
"""

from __future__ import annotations

import json

import httpx
import pytest

from core.usage_sniffer import CostSink, sniffing_client


def sse(*, cost: float | None, gen_id: str = "gen-abc") -> str:
    def frame(**choice) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "id": gen_id,
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "openai/gpt-4o-mini",
                    "choices": [dict(index=0, **choice)],
                }
            )
            + "\n\n"
        )

    body = frame(delta={"role": "assistant", "content": "hi "}) + frame(delta={"content": "there"})
    body += frame(delta={}, finish_reason="stop")
    usage: dict[str, object] = {"prompt_tokens": 50, "completion_tokens": 12, "total_tokens": 62}
    if cost is not None:
        usage["cost"] = cost
    body += (
        "data: "
        + json.dumps(
            {
                "id": gen_id,
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "openai/gpt-4o-mini",
                "choices": [],
                "usage": usage,
            }
        )
        + "\n\n"
    )
    return body + "data: [DONE]\n\n"


def json_body(cost: float) -> dict:
    return {
        "id": "gen-xyz",
        "object": "chat.completion",
        "created": 0,
        "model": "openai/gpt-4o-mini",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7, "cost": cost},
    }


def streaming(*bodies: str):
    """A MockTransport serving ``bodies`` as event streams, in order."""
    remaining = list(bodies)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=remaining.pop(0),
            headers={"content-type": "text/event-stream"},
        )

    return httpx.MockTransport(handler)


def drain(client: httpx.Client, url: str = "https://openrouter.ai/api/v1/chat/completions") -> str:
    """Read a streaming response the way the openai SDK does — and stop at [DONE].

    Stopping early is the point: it is what abandons the generator and what
    broke the first implementation.
    """
    received = []
    with client.stream("POST", url, json={"stream": True}) as response:
        for line in response.iter_lines():
            received.append(line)
            if line.strip() == "data: [DONE]":
                break
    return "\n".join(received)


# --------------------------------------------------------------------------
# streaming
# --------------------------------------------------------------------------


def test_the_cost_is_recovered_from_a_streamed_response():
    sink = CostSink()
    client = sniffing_client(sink, transport=streaming(sse(cost=0.00011)))

    drain(client)

    assert sink.total_usd == pytest.approx(0.00011)
    assert sink.observations == 1


def test_the_body_passes_through_byte_for_byte():
    """The SDK must still parse the stream; a sniffer that corrupts it is worse."""
    body = sse(cost=0.00011)
    sink = CostSink()
    client = sniffing_client(sink, transport=streaming(body))

    received = drain(client)

    assert "hi " in received and "there" in received
    assert received.count("data:") == body.count("data:")


def test_the_cost_is_seen_even_though_the_reader_stops_at_done():
    """The regression this file exists for: post-loop scanning never runs."""
    sink = CostSink()
    client = sniffing_client(sink, transport=streaming(sse(cost=0.0004)))

    with client.stream("POST", "https://openrouter.ai/api/v1/chat/completions", json={}) as r:
        for line in r.iter_lines():
            if line.strip() == "data: [DONE]":
                break  # abandons the generator, exactly as the SDK does

    assert sink.total_usd == pytest.approx(0.0004)


def test_costs_accumulate_over_several_calls():
    """One turn makes several model calls; each one's charge must be counted."""
    sink = CostSink()
    client = sniffing_client(sink, transport=streaming(sse(cost=0.001), sse(cost=0.002)))

    drain(client)
    drain(client)

    assert sink.total_usd == pytest.approx(0.003)
    assert sink.observations == 2


def test_a_stream_without_a_cost_field_contributes_nothing():
    """Some models report no cost. That is zero, not a crash."""
    sink = CostSink()
    client = sniffing_client(sink, transport=streaming(sse(cost=None)))

    drain(client)

    assert sink.total_usd == 0.0
    assert sink.observations == 0


def test_a_frame_split_across_two_reads_is_still_parsed():
    """Chunk boundaries are not line boundaries on a real socket."""
    body = sse(cost=0.00025)
    half = len(body) // 2
    parts = [body[:half].encode(), body[half:].encode()]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=httpx._content.IteratorByteStream(iter(parts)),
            headers={"content-type": "text/event-stream"},
        )

    sink = CostSink()
    client = sniffing_client(sink, transport=httpx.MockTransport(handler))

    drain(client)

    assert sink.total_usd == pytest.approx(0.00025)


# --------------------------------------------------------------------------
# non-streaming
# --------------------------------------------------------------------------


def test_the_cost_is_recovered_from_a_plain_json_response():
    """Structured-output calls are not streamed; they must be counted too."""
    sink = CostSink()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=json_body(0.00007))

    client = sniffing_client(sink, transport=httpx.MockTransport(handler))

    response = client.post("https://openrouter.ai/api/v1/chat/completions", json={})

    assert response.json()["choices"][0]["message"]["content"] == "hi"
    assert sink.total_usd == pytest.approx(0.00007)


def test_a_non_json_response_is_ignored_rather_than_raising():
    sink = CostSink()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream is down")

    client = sniffing_client(sink, transport=httpx.MockTransport(handler))
    client.post("https://openrouter.ai/api/v1/chat/completions", json={})

    assert sink.total_usd == 0.0


# --------------------------------------------------------------------------
# the sink
# --------------------------------------------------------------------------


def test_taking_the_total_resets_it():
    """A turn drains the sink so the next turn starts from zero."""
    sink = CostSink()
    sink.record(0.001)
    sink.record(0.002)

    assert sink.take() == pytest.approx(0.003)
    assert sink.take() == 0.0
    assert sink.observations == 0


def test_a_negative_cost_is_ignored():
    """Defensive: a nonsense figure must not reduce a real total."""
    sink = CostSink()
    sink.record(0.001)
    sink.record(-5.0)
    assert sink.total_usd == pytest.approx(0.001)


# --------------------------------------------------------------------------
# through the real LangChain streaming stack
# --------------------------------------------------------------------------


def test_cost_is_recovered_through_langchains_streaming_path():
    """The assertion this module exists for, end to end.

    Asserts both halves of the split: LangChain gives correct *tokens* and no
    cost, and the sniffer supplies the cost LangChain dropped.
    """
    from langchain_openai import ChatOpenAI

    sink = CostSink()
    model = ChatOpenAI(
        model="openai/gpt-4o-mini",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        extra_body={"usage": {"include": True}},
        http_client=sniffing_client(sink, transport=streaming(sse(cost=0.00011))),
    )

    text = ""
    aggregate = None
    for chunk in model.stream("what does sws_can_11 require?"):
        text += chunk.content or ""
        aggregate = chunk if aggregate is None else aggregate + chunk

    assert text == "hi there", "streaming must still work"
    assert aggregate.usage_metadata["input_tokens"] == 50, "LangChain supplies tokens"
    assert "token_usage" not in (aggregate.response_metadata or {}), (
        "this is the gap: LangChain drops the provider's raw usage on the streaming path"
    )
    assert sink.total_usd == pytest.approx(0.00011), "and the sniffer fills it"
