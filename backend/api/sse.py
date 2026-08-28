"""SSE plumbing shared by every stream this API serves.

Three endpoints stream server-sent events — the chat turn, report progress
and ingestion progress — and each had grown its own copy of the same three
things: the ``data: {type, data}`` frame, the anti-buffering headers, and the
"follow a queue with keep-alives" loop. Copies drift (the two follow loops
had already been retyped rather than shared); the protocol now lives here and
the endpoints keep only what genuinely differs — their payload shapes.

The chat stream's frames stay in ``api/chat_events.py``: they are the typed
wire contract mirrored by ``events.ts``, built from Pydantic models rather
than plain dicts. The envelope is the same ``{type, data}``.
"""

from __future__ import annotations

import json
import queue
from collections.abc import Callable, Iterator
from typing import Any

#: How long a stalled events stream waits before emitting a keep-alive comment.
#: Comfortably under the ~60 s idle timeouts proxies apply, and long enough to
#: cost nothing when events flow.
HEARTBEAT_SECONDS = 15.0

#: A comment frame. Keeps the connection alive through long quiet stretches
#: (a PDF download, a slow judge call); clients ignore it by SSE rules.
KEEP_ALIVE = ": keep-alive\n\n"

SSE_MEDIA_TYPE = "text/event-stream"

#: Streaming through a proxy: without these, an intermediary may buffer the
#: whole response and everything arrives at once at the end.
SSE_HEADERS = {"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"}


def frame(kind: str, data: dict) -> str:
    """One SSE frame in the ``{type, data}`` envelope the chat stream uses.

    A single ``data:`` line of JSON terminated by a blank line; JSON escapes
    newlines, so a payload containing a blank line cannot end its own frame.
    """
    return f"data: {json.dumps({'type': kind, 'data': data})}\n\n"


def follow(
    events: queue.Queue,
    *,
    item_frame: Callable[[Any], str],
    final_frame: Callable[[], str],
    heartbeat: float = HEARTBEAT_SECONDS,
    finished: Callable[[], bool] | None = None,
) -> Iterator[str]:
    """Frames for a live queue: items as they arrive, keep-alives while quiet.

    The producer signals completion by putting ``None``; that yields
    ``final_frame()`` (built at that moment, so it reports the terminal state)
    and ends the stream. Callers replay whatever a late-connecting client
    should see *before* delegating here — replay is where the streams differ.

    ``finished`` guards against a stolen sentinel: the queue is
    single-consumer, so when two readers follow one run — a dev-mode remount
    reconnecting, a second tab — the ``None`` reaches only one of them.
    A caller that can ask its producer "are you done?" passes that here, and
    the other reader ends on its next heartbeat instead of keeping the
    connection alive forever.
    """
    while True:
        try:
            item = events.get(timeout=heartbeat)
        except queue.Empty:
            if finished is not None and finished():
                yield final_frame()
                return
            yield KEEP_ALIVE
            continue
        if item is None:
            yield final_frame()
            return
        yield item_frame(item)
