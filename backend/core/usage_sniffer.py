"""Reading OpenRouter's real cost off the wire (finding D1, story S3.1.1).

Finding D1 says: do not build a price table, report the provider's own
``usage.cost``. For non-streaming calls :mod:`core.llm` reads it straight out of
``response_metadata["token_usage"]["cost"]``, which langchain-openai copies
verbatim.

**On the streaming path it is not there.** Measured against langchain-openai
1.6.0: a streamed reply's token counts reach
``AIMessageChunk.usage_metadata``, but the provider's raw usage dict is dropped
and ``response_metadata`` on the usage chunk is ``{}``. ``stream_usage=True``
changes the request (it adds ``stream_options.include_usage``) but not what
survives the parse. So the one number that must not be estimated is the one
number the abstraction discards — and it belongs to the chat model, which is
streamed because the streaming answer *is* the product, and which is the
largest cost in a turn.

This module recovers it from the HTTP response itself. That is a layer below
LangChain, but it does not fight it: the project already injects its own
``http_client`` (the tests depend on that), so the sniffing transport composes
with whatever is underneath — a real connection pool in production, an
``httpx.MockTransport`` in tests.

**The subtlety that needs a test, not a comment.** The openai SDK stops reading
the body at ``data: [DONE]`` and abandons the generator. Anything that scans the
buffer *after* the iteration loop therefore never runs, so parsing is done
incrementally, line by line, as bytes go past. The first version of this scanned
at the end, sniffed nothing, and passed every other assertion.

Alternatives considered and rejected: pricing tokens from a table (forbidden by
D1); calling OpenRouter's ``/generation?id=`` endpoint afterwards (an extra
round trip per turn, and the figure lags); subclassing ``ChatOpenAI`` to
preserve the raw usage (fragile against the next release, and it would have to
be re-verified anyway).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx

#: Response content types treated as event streams rather than a single JSON
#: document. Matched as a prefix so ``text/event-stream; charset=utf-8`` counts.
_STREAM_CONTENT_TYPE = "text/event-stream"

#: SSE payloads that carry no JSON object.
_SENTINELS = frozenset({"", "[DONE]"})


@dataclass
class CostSink:
    """Collects the charges observed on one client's responses.

    Held per request rather than per process: the API builds its retrieval
    engine per request anyway (see ``retrieval.pipeline.Engine``'s warning), so
    a sink cannot accumulate two users' turns into one figure.
    """

    total_usd: float = 0.0
    observations: int = 0
    #: Every charge seen, in order. Kept for the per-turn stage log — "the
    #: answer cost $0.006 over four calls" is more useful than one number.
    charges: list[float] = field(default_factory=list)

    def record(self, cost: float) -> None:
        """Add one observed charge. Nonsense figures are ignored."""
        if cost <= 0.0:
            return
        self.total_usd += cost
        self.observations += 1
        self.charges.append(cost)

    def take(self) -> float:
        """Return the total and reset, so the next turn starts from zero."""
        total = self.total_usd
        self.total_usd = 0.0
        self.observations = 0
        self.charges.clear()
        return total


def _cost_of(payload: str) -> float | None:
    """``usage.cost`` from one JSON document, or ``None``."""
    try:
        body = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    return float(cost) if isinstance(cost, int | float) else None


class _SniffingTransport(httpx.BaseTransport):
    """Passes every response through untouched, recording ``usage.cost``."""

    def __init__(self, sink: CostSink, inner: httpx.BaseTransport) -> None:
        self._sink = sink
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        content_type = response.headers.get("content-type", "")
        if _STREAM_CONTENT_TYPE in content_type:
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=httpx._content.IteratorByteStream(self._tee(response.stream)),
                request=request,
                extensions=response.extensions,
            )
        return self._sniff_json(response, request)

    def _sniff_json(self, response: httpx.Response, request: httpx.Request) -> httpx.Response:
        """Read a whole (non-streaming) body, record its cost, hand it back."""
        body = response.read()
        cost = _cost_of(body.decode("utf-8", "replace"))
        if cost is not None:
            self._sink.record(cost)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=body,
            request=request,
            extensions=response.extensions,
        )

    def _tee(self, source) -> Iterator[bytes]:
        """Yield ``source``'s bytes unchanged, parsing ``data:`` lines as they pass.

        Parsed *incrementally*. The SDK breaks out of its read loop at
        ``[DONE]``, which abandons this generator — so any work deferred to
        after the loop would never happen.
        """
        pending = bytearray()
        for part in source:
            pending.extend(part)
            while b"\n" in pending:
                raw, _, rest = bytes(pending).partition(b"\n")
                pending = bytearray(rest)
                self._observe(raw)
            yield part

    def _observe(self, raw: bytes) -> None:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            return
        payload = line[len("data:") :].strip()
        if payload in _SENTINELS:
            return
        cost = _cost_of(payload)
        if cost is not None:
            self._sink.record(cost)


def sniffing_client(
    sink: CostSink,
    *,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 120.0,
) -> httpx.Client:
    """An ``httpx.Client`` that records every ``usage.cost`` it sees into ``sink``.

    Pass ``transport`` to wrap something other than a real connection pool —
    that is how the tests substitute an ``httpx.MockTransport`` and prove no
    call reaches OpenRouter.
    """
    inner = transport if transport is not None else httpx.HTTPTransport()
    return httpx.Client(transport=_SniffingTransport(sink, inner), timeout=timeout)
