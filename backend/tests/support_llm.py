"""A fake OpenRouter, for every test that needs an LLM reply.

CLAUDE.md forbids tests reaching OpenRouter, and the cheapest way to *prove*
they do not is to intercept at the HTTP layer rather than to mock a Python
object. :class:`FakeOpenRouter` installs an ``httpx.MockTransport`` under the
real ``openai`` SDK, so:

* no socket is ever opened — a request to an unscripted endpoint raises
  instead of escaping to the network;
* the whole real code path runs (LangChain builds the payload, the SDK
  serialises it, the SDK parses the reply), so a test asserts on the *actual
  wire request* — which is what caught two things a Python-level mock would
  have hidden: that ``method="json_schema"`` takes a different SDK code path
  than the default one, and that the SDK validates a Pydantic
  ``response_format`` itself before LangChain's parser ever sees it;
* replies carry OpenRouter's own ``usage.cost`` field, which is where the
  real charge lives (finding D1 — do not build a price table).

Use :func:`fake_llm` for the common case; reach for :class:`FakeOpenRouter`
directly only when a test needs to assert on the raw request bodies.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from core.llm import Llm, chat_model
from core.manifest import ProjectManifest

#: What the fake claims to be. Only echoed back, never used to pick a price.
DEFAULT_MODEL_ID = "openai/gpt-4o-mini"


def openrouter_body(
    content: str,
    *,
    cost_usd: float = 0.0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: str = DEFAULT_MODEL_ID,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    """One OpenRouter chat-completion response body.

    ``usage.cost`` is included because that is the field this project bills
    from; a fake that omitted it would let a regression in cost accounting
    pass every test.
    """
    return {
        "id": "gen-fake",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost": cost_usd,
        },
    }


def json_body(payload: Any, **kwargs: Any) -> dict[str, Any]:
    """:func:`openrouter_body` whose content is ``payload`` as JSON."""
    return openrouter_body(json.dumps(payload), **kwargs)


class FakeOpenRouter:
    """Serves a scripted list of replies over a transport that has no network.

    Each reply is either a response body (see :func:`openrouter_body`) or an
    ``httpx.Response`` for tests that need a status code — a 429 to exercise
    a retry, say.
    """

    def __init__(self, *replies: dict[str, Any] | httpx.Response) -> None:
        self.replies: list[dict[str, Any] | httpx.Response] = list(replies)
        #: The parsed JSON body of every request, in order.
        self.requests: list[dict[str, Any]] = []
        #: The path of every request, in order.
        self.paths: list[str] = []

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        self.requests.append(json.loads(request.content))
        if not self.replies:
            raise AssertionError(
                f"unscripted request number {len(self.requests)} to {request.url.path} — "
                "the fake ran out of replies, which means the code under test made more "
                "calls than expected"
            )
        reply = self.replies.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        return httpx.Response(200, json=reply)

    def transport(self) -> httpx.MockTransport:
        """The bare transport, for composing with a wrapper.

        The cost sniffer (:mod:`core.usage_sniffer`) wraps a transport, and
        production wraps the real one exactly this way — so a test that wants
        cost recovery composes the two rather than faking the sniffer.
        """
        return httpx.MockTransport(self._handle)

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())

    @property
    def calls(self) -> int:
        return len(self.requests)

    def messages_of(self, index: int) -> list[dict[str, Any]]:
        """The ``messages`` array of request ``index``."""
        return self.requests[index]["messages"]

    def prompt_of(self, index: int) -> str:
        """Every message of request ``index``, concatenated — for substring asserts."""
        return "\n".join(str(m.get("content", "")) for m in self.messages_of(index))


def fake_llm(
    manifest: ProjectManifest,
    *replies: dict[str, Any] | httpx.Response,
    purpose: str = "rerank",
    **kwargs: Any,
) -> tuple[Llm, FakeOpenRouter]:
    """An :class:`~core.llm.Llm` wired to a :class:`FakeOpenRouter`.

    The key is a placeholder: the transport never authenticates, and passing
    one explicitly keeps the test independent of the developer's ``.env``.
    """
    fake = FakeOpenRouter(*replies)
    llm = chat_model(
        manifest,
        purpose,
        api_key="sk-test-not-a-real-key",
        http_client=fake.http_client(),
        **kwargs,
    )
    return llm, fake
