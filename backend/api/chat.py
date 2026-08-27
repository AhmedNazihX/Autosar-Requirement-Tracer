"""``POST /chat`` — the streaming answer (stories S3.3.1, S3.3.3, S3.5.1).

The response is SSE in the ``{type, data}`` envelope of
``frontend/lib/events.ts``, framed by :func:`api.chat_events.sse_frame`.

**The events are the message** (spec §11). Nothing else about an assistant turn
is persisted, because everything visible about it — streamed text, tool chips,
citations, the cost line, an error banner — is a pure function of its event
array (``frontend/lib/event-reducer.ts``). So reloading a thread replays the
stored events and produces a byte-identical render, and the live path and the
reload path go through the same data. Storing a summary alongside would create
a second truth that could disagree.

**Persistence happens after the stream closes, not during.** Writing each event
as it arrived would put a SQLite write between the model and the browser on
every token. So events are collected while streaming and written once at the
end — including when the turn failed, because a failed turn is still part of the
conversation and the frontend keeps what it received.

**A thread is created on demand.** The client generates its own thread id and
sends it; if the backend has not seen it, it creates it. That keeps the frontend
able to open a new thread without a round trip first, and makes ``POST /chat``
idempotent about thread existence.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field

from agent.runner import run_turn
from agent.tools import SideChannel, ToolContext
from api import deps, sse
from api import reports as api_reports
from api import threads as api_threads
from api.chat_events import ChatEvent, ChatEventEnvelope, ErrorEvent, sse_frame
from api.ratelimit import RateLimiter, client_key
from core import db
from core.config import get_settings

router = APIRouter(tags=["chat"])

#: Longest message accepted (story S6.3.1's length cap). Generous for a
#: question; far below a pasted document, which is what a much larger body is.
MAX_MESSAGE_CHARS = 8000

#: How many prior messages of a thread are replayed to the model. The whole
#: history would grow the prompt without bound; a traceability conversation
#: refers back a few turns at most, and every answer is re-grounded by tools.
HISTORY_TURNS = 12

#: The token bucket in front of this endpoint (story S6.3.1). Module-level
#: because the limit is a property of the process, not of a request: one
#: bucket per client, shared by every worker thread uvicorn runs. Sized from
#: settings so a flood test can rebuild it with its own numbers.
LIMITER = RateLimiter(
    per_minute=get_settings().chat_rate_limit_per_minute,
    burst=get_settings().chat_rate_limit_burst,
)

#: Sent when the corpus is not loaded. Names the fix, because at this point the
#: user has a running backend and an empty index, which looks like a bug.
NOT_READY_MESSAGE = (
    "The corpus is not indexed yet, so there is nothing to answer from. Run "
    "'uv run python -m ingestion.run ../projects/autosar-can/project.yaml' in "
    "backend/, then restart the API."
)


class ChatRequest(BaseModel):
    """The body ``frontend/lib/chat-sources.ts`` posts."""

    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


@router.post("/chat")
def chat(
    body: ChatRequest,
    request: Request,
    state: deps.AppState = Depends(deps.state_of),
) -> StreamingResponse:
    """Answer ``body.message`` as a stream of SSE events.

    Always returns 200 with a stream: a failure the client can render as a
    message is more useful than a status code it has to interpret, and
    ``chat-sources.ts`` turns a non-2xx into a generic error while an ``error``
    event carries a real sentence. The exceptions are a malformed body (422,
    from validation), a rate-limited client (429, below), and an unindexed
    corpus — which is reported as an ``error`` event for the same reason.
    """
    # Before anything else, including the readiness check: a client looping
    # against an unindexed backend is the same runaway, and the point of the
    # bucket is to make the cheap answer cheap.
    wait = LIMITER.check(client_key(request))
    if wait > 0:
        # A status code rather than an `error` event, which is the exception to
        # this endpoint's "always stream" rule: 429 is the one failure a
        # well-behaved client is supposed to *act* on, and `Retry-After` tells
        # it by how much. `chat-sources.ts` renders it as a message anyway.
        raise HTTPException(
            status_code=429,
            detail=(
                "Too many questions at once. This is a local tool talking to a "
                "paid model, so the backend paces requests rather than letting "
                "a loop run up a bill."
            ),
            headers={"Retry-After": str(max(1, int(wait + 0.999)))},
        )

    if not state.ready:
        return _stream([ErrorEvent(message=NOT_READY_MESSAGE, code="not_indexed")])

    return StreamingResponse(
        _turn(state, body),
        media_type="text/event-stream",
        headers={
            # Streaming through a proxy: without these, an intermediary may
            # buffer the whole response and the answer arrives all at once.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def _turn(state: deps.AppState, body: ChatRequest) -> Iterator[str]:
    """Run one turn, yielding SSE frames and persisting the result at the end."""
    message = body.message.strip()
    events: list[ChatEvent] = []

    try:
        turn = deps.build_turn(state)
        context = ToolContext.build(
            turn.engine,
            side=SideChannel(),
            judge_llm=turn.judge,
            # The report tool starts a thread rather than a BackgroundTask:
            # a task runs after the response, and this response is a stream
            # that is still open when the tool is called.
            launch_report=lambda scope: api_reports.launch_in_thread(state, scope),
        )
        history = _history(state, body.thread_id)
    except Exception as exc:  # noqa: BLE001 - setup failures must reach the user
        failure = ErrorEvent(
            message=f"The backend could not start this answer: {exc}",
            code="turn_setup_failed",
            retryable=False,
        )
        yield sse_frame(failure)
        _persist(state, body.thread_id, message, [failure], titler=None)
        return

    try:
        for event in run_turn(
            context, turn.chat, message, history=history, cost_sink=turn.cost_sink
        ):
            events.append(event)
            yield sse_frame(event)
    except Exception as exc:  # noqa: BLE001 - the stream must not die silently
        # run_turn already converts model failures into `error` events, so
        # reaching here means something unexpected broke. The client still gets
        # a terminal event, and the partial message is still stored.
        failure = ErrorEvent(
            message=(
                "The answer stopped unexpectedly. Anything already shown is kept "
                "in this thread."
            ),
            code=f"stream_{type(exc).__name__}",
            retryable=True,
        )
        events.append(failure)
        yield sse_frame(failure)

    _persist(state, body.thread_id, message, events, titler=turn.titler)


# --------------------------------------------------------------------------
# persistence
# --------------------------------------------------------------------------


def _history(state: deps.AppState, thread_id: str) -> list[BaseMessage]:
    """The thread's recent turns, as LangChain messages.

    Reconstructed from stored ``content`` rather than by replaying events: the
    model needs the text, and ``content`` is exactly that (see
    ``core.db.create_message``). Tool calls are deliberately not replayed —
    each turn re-grounds itself through its own tool calls, and replaying stale
    tool output would let an old retrieval answer a new question.
    """
    if state.pool is None:
        return []
    stored = db.list_messages(state.pool, thread_id)[-HISTORY_TURNS:]
    history: list[BaseMessage] = []
    for record in stored:
        text = (record.get("content") or "").strip()
        if not text:
            continue
        history.append(
            HumanMessage(content=text)
            if record["role"] == "user"
            else AIMessage(content=text)
        )
    return history


def _persist(
    state: deps.AppState,
    thread_id: str,
    message: str,
    events: list[ChatEvent],
    *,
    titler,
) -> None:
    """Store the user message and the assistant's event stream.

    Never raises: a failed write must not turn a delivered answer into an
    error, and the client already has the events.
    """
    if state.pool is None or state.manifest is None:
        return
    try:
        if db.get_thread(state.pool, thread_id) is None:
            db.create_thread_with_id(
                state.pool, thread_id, state.manifest.project_id, title=None
            )
        user_id = db.create_message(
            state.pool, thread_id, role="user", events=[], content=message
        )
        db.create_message(
            state.pool,
            thread_id,
            role="assistant",
            events=[ChatEventEnvelope.of(event).model_dump(mode="json") for event in events],
            content=_answer_text(events),
            parent_message_id=user_id,
        )
    except Exception:  # noqa: BLE001 - the answer was already delivered
        return
    # Story S3.5.3: name the thread from its first message. Best-effort — a
    # failure here leaves it untitled, which the sidebar already handles.
    api_threads.autotitle(state, thread_id, message, titler)


def _answer_text(events: list[ChatEvent]) -> str:
    """The assistant's text, joined from its ``token`` events."""
    return "".join(event.text for event in events if event.EVENT_TYPE == "token")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _stream(events: list[ChatEvent]) -> StreamingResponse:
    """A one-shot SSE response, for a failure known before the turn starts."""

    def body() -> Iterator[str]:
        for event in events:
            yield sse_frame(event)

    return StreamingResponse(
        body(),
        media_type=sse.SSE_MEDIA_TYPE,
        headers=sse.SSE_HEADERS,
    )
