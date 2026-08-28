"""One agent turn, as a stream of SSE events (story S3.2.2).

``langchain.agents.create_agent`` runs the tool-calling loop — it is the locked
stack's own agent, and WP4's two extra tools plug into it by being in the
registry. This module's job is the *translation*: LangGraph emits its own stream
shapes, the frontend consumes the ``{type, data}`` contract in
``frontend/lib/events.ts``, and nothing should map between them twice.

**Two stream modes, because one is not enough.** ``messages`` yields
``AIMessageChunk``s, which is where token deltas and per-call usage live;
``updates`` yields completed node output, which is where a resolved
``AIMessage.tool_calls`` and the ``ToolMessage`` results live. Verified against
langchain 1.3.17 by running it: a tool call's arguments arrive in ``messages``
only as partial JSON fragments, so ``tool_start`` is emitted from the
``updates`` view where the arguments are parsed and complete.

**Citations do not come from the model.** They come from the tools' side
channel (:class:`agent.tools.SideChannel`), keyed by the same ``tool_call_id``
the ``tool_start``/``tool_result`` events correlate on. Spec §6 requires that:
an id the model paraphrased would produce a citation pointing at the wrong
requirement, and it would look completely fine.

**Every turn terminates, exactly once, with usage before it.** The frontend
reducer labels a stream with no ``done``/``error`` as ``interrupted``
(``event-reducer.ts:139``) and keeps only the last ``usage`` event it sees
(``:120``). So the ordering here — tokens and tool events, then one aggregate
``usage``, then ``done`` or ``error`` — is a contract, not a style choice.

**An error is additive.** Whatever streamed before a failure is part of the
thread and stays; the ``error`` event is appended, and the usage already
incurred is still reported, because a failed turn still cost money.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage

from agent.prompts import system_prompt
from agent.tools import SideChannel, ToolContext, build_tools
from api.chat_events import (
    ChatEvent,
    DoneEvent,
    ErrorEvent,
    TokenEvent,
    ToolResultEvent,
    ToolStartEvent,
    TurnMeter,
)
from core.llm import Llm, LlmUsage
from core.usage_sniffer import CostSink

#: What the user is told when the model or its transport fails. Deliberately
#: says what survived: the frontend keeps the partial message, and a user who
#: is not told that assumes it was lost.
TRANSPORT_ERROR_MESSAGE = (
    "The answer stopped early: the language model could not be reached. "
    "Anything already shown above is kept in this thread — send the message "
    "again to retry."
)

#: Cap on tool-calling rounds. ``create_agent`` will otherwise loop as long as
#: the model keeps asking, and a model that has decided to search forever is a
#: cost incident rather than a useful answer.
DEFAULT_RECURSION_LIMIT = 12


def run_turn(
    context: ToolContext,
    chat: Llm,
    message: str,
    *,
    history: list[BaseMessage] | None = None,
    cost_sink: CostSink | None = None,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> Iterator[ChatEvent]:
    """Answer ``message``, yielding SSE events in the order they should be sent.

    A generator, so the endpoint can forward each event as it happens rather
    than assembling the whole answer first — the streaming answer is the
    product.

    ``cost_sink`` is the :mod:`core.usage_sniffer` sink attached to ``chat``'s
    HTTP client. When given, its figures are the authority on cost: LangChain
    drops OpenRouter's ``usage.cost`` on the streaming path, so the token counts
    come from LangChain and the money comes from the wire.
    """
    if not message.strip():
        raise ValueError("cannot answer an empty message")

    meter = TurnMeter(model=chat.model_id)
    started = time.perf_counter()
    tools = build_tools(context)
    agent = create_agent(chat.model, tools, system_prompt=system_prompt(context.engine.manifest))

    conversation: list[BaseMessage | dict[str, Any]] = list(history or [])
    conversation.append({"role": "user", "content": message})

    #: tool_call_id -> when its `tool_start` was emitted, for duration_ms.
    started_at: dict[str, float] = {}
    #: tool_call_id -> the name we announced, so a result names the same tool.
    announced: dict[str, str] = {}
    failed: ErrorEvent | None = None

    try:
        for mode, payload in agent.stream(
            {"messages": conversation},
            stream_mode=["updates", "messages"],
            config={"recursion_limit": recursion_limit},
        ):
            if mode == "messages":
                yield from _from_message_chunk(payload, meter, chat)
            else:
                yield from _from_updates(
                    payload, context.side, started_at, announced, meter
                )
    except Exception as exc:  # noqa: BLE001 - every failure must reach the user
        failed = ErrorEvent(
            message=TRANSPORT_ERROR_MESSAGE,
            code=f"model_{type(exc).__name__}",
            retryable=True,
        )

    # Any tool that never produced a result — the stream died between the call
    # and its outcome. Saying so beats a chip that spins forever.
    for call_id, tool_name in announced.items():
        if call_id in started_at:
            yield _unresolved_result(call_id, tool_name, started_at[call_id])

    if cost_sink is not None:
        meter.add_wire_cost(cost_sink.take())

    yield meter.usage_event(elapsed_ms=int((time.perf_counter() - started) * 1000))
    yield failed if failed is not None else DoneEvent(finish_reason="stop")


# --------------------------------------------------------------------------
# mapping LangGraph's two stream modes
# --------------------------------------------------------------------------


def _from_message_chunk(payload: Any, meter: TurnMeter, chat: Llm) -> Iterator[ChatEvent]:
    """Token deltas and per-call usage, from the ``messages`` stream mode.

    Tool-call fragments are ignored here: in this mode a call's arguments
    arrive as partial JSON, so announcing it would mean re-implementing the
    accumulation ``updates`` has already done.
    """
    chunk = payload[0] if isinstance(payload, tuple) else payload
    if not isinstance(chunk, AIMessageChunk):
        return

    usage = getattr(chunk, "usage_metadata", None)
    if usage:
        meter.add_llm(_llm_usage(usage, chat))

    text = _text_of(chunk)
    if text:
        yield TokenEvent(text=text)


def _from_updates(
    payload: dict[str, Any],
    side: SideChannel,
    started_at: dict[str, float],
    announced: dict[str, str],
    meter: TurnMeter,
) -> Iterator[ChatEvent]:
    """Tool starts and results, from the ``updates`` stream mode."""
    for update in payload.values():
        if not isinstance(update, dict):
            continue
        for item in update.get("messages") or []:
            if isinstance(item, AIMessage):
                yield from _tool_starts(item, started_at, announced)
            elif isinstance(item, ToolMessage):
                yield from _tool_result(item, side, started_at, announced, meter)


def _tool_starts(
    message: AIMessage, started_at: dict[str, float], announced: dict[str, str]
) -> Iterator[ChatEvent]:
    for call in message.tool_calls or []:
        call_id = call.get("id") or ""
        name = call.get("name") or ""
        if not call_id or call_id in announced:
            # A duplicate id would overwrite a chip (`event-reducer.ts:86`
            # keeps the first), so it is never announced twice.
            continue
        announced[call_id] = name
        started_at[call_id] = time.perf_counter()
        yield ToolStartEvent(id=call_id, tool=name, args=dict(call.get("args") or {}))


def _tool_result(
    message: ToolMessage,
    side: SideChannel,
    started_at: dict[str, float],
    announced: dict[str, str],
    meter: TurnMeter,
) -> Iterator[ChatEvent]:
    call_id = message.tool_call_id
    if call_id not in announced:
        # A result with no announced start would be dropped by the reducer
        # anyway (`event-reducer.ts:103`); emitting it would be noise.
        return
    duration_ms = _elapsed_ms(started_at.pop(call_id, None))
    outcome = side.outcome(call_id)

    if outcome is None:
        # The tool ran but recorded nothing — a tool that forgot its side
        # channel. Report the call as done rather than leaving a chip pending.
        yield ToolResultEvent(
            id=call_id, status="ok", summary="completed", duration_ms=duration_ms
        )
        return

    if outcome.usage is not None:
        # A search runs its own models (rewriter, reranker) and embeds its
        # queries. That is part of what the answer cost.
        meter.add_pipeline(outcome.usage)

    yield ToolResultEvent(
        id=call_id,
        status="ok" if outcome.ok else "error",
        summary=outcome.summary,
        duration_ms=duration_ms,
        error=outcome.error,
        stages=list(outcome.stages) if outcome.stages else None,
        job_id=outcome.job_id,
    )
    # Citations follow their tool_result: the chip exists before anything tries
    # to point into it.
    yield from outcome.citations


def _unresolved_result(call_id: str, tool_name: str, started: float) -> ToolResultEvent:
    return ToolResultEvent(
        id=call_id,
        status="error",
        summary="did not finish",
        duration_ms=_elapsed_ms(started),
        error=(
            f"The {tool_name} call did not finish before the answer stopped. "
            "Send the message again to retry."
        ),
    )


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _elapsed_ms(started: float | None) -> int:
    return 0 if started is None else max(int((time.perf_counter() - started) * 1000), 0)


def _llm_usage(usage: dict[str, Any], chat: Llm) -> LlmUsage:
    return LlmUsage(
        purpose=chat.purpose,
        calls=1,
        prompt_tokens=int(usage.get("input_tokens", 0) or 0),
        completion_tokens=int(usage.get("output_tokens", 0) or 0),
        total_tokens=int(usage.get("total_tokens", 0) or 0),
    )


def _text_of(chunk: AIMessageChunk) -> str:
    content = chunk.content
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
