"""Thread CRUD and auto-titling (stories S3.5.2, S3.5.3).

The shapes here match ``frontend/lib/threads.ts`` exactly — ``Thread``,
``ThreadSummary``, ``StoredMessage`` — because that module already describes the
five endpoints it will call and the payloads it expects. It is currently
localStorage-backed; these endpoints are what it swaps to, so forking the shapes
would turn a one-line change into a migration.

**A message is its event stream** (spec §11). ``GET /threads/{id}`` returns each
message's stored events verbatim, and the frontend's reducer rebuilds the whole
render from them — tool chips, citations, cost line, error banners. ``content``
travels alongside for exports and titles, and is never the render source.

**Auto-titling is best-effort and cheap.** It runs on the manifest's ``title``
model after the first exchange, and a failure leaves the thread untitled rather
than failing the request: a thread with a dull name is a cosmetic problem, and
the frontend already falls back to the first user message.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from api import deps
from core import db, llm
from core.llm import system, user
from retrieval.prompting import fence

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads", tags=["threads"])

#: Longest generated title. The sidebar truncates anyway, and a model asked for
#: "a short title" will occasionally write a sentence.
MAX_TITLE_CHARS = 72

#: How much of the first message the titler is shown. A title needs the
#: opening sentence, not a pasted document — and a smaller prompt is a smaller
#: surface for a title injection to work in.
MAX_TITLE_SOURCE_CHARS = 2000

#: What an untitled thread is called — the same string ``threads.ts`` uses, so
#: the two sides agree on what "not yet titled" looks like.
UNTITLED = "New thread"

#: The fence the user's own message is wrapped in before the titler sees it.
#: Named so story S6.2.1 can assert on it.
MESSAGE_FENCE = "<<<FIRST_MESSAGE>>>"

TITLE_PROMPT = (
    "Write a title of at most eight words for a conversation that begins with "
    "the message below. It is a question about automotive software "
    "specifications. Reply with the title only — no quotes, no punctuation at "
    "the end, no preamble."
)


class ThreadSummary(BaseModel):
    """A row in the sidebar."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    created_at: str
    updated_at: str
    message_count: int


class StoredMessage(BaseModel):
    """One stored message: its text and its complete event stream."""

    model_config = ConfigDict(extra="forbid")

    id: str
    role: str
    content: str
    events: list[dict] = Field(default_factory=list)
    created_at: str
    parent_id: str | None = None


class Thread(BaseModel):
    """A thread with its messages, ready to replay."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    created_at: str
    updated_at: str
    messages: list[StoredMessage] = Field(default_factory=list)


class CreateThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=MAX_TITLE_CHARS * 2)
    #: The client may bring its own id so it can open a thread without waiting.
    id: str | None = Field(default=None, max_length=128)


class RenameThreadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS * 2)


def _ready(state: deps.AppState) -> deps.AppState:
    if state.pool is None or state.manifest is None:
        raise HTTPException(
            status_code=503,
            detail=state.error or "The corpus is not indexed yet.",
        )
    return state


def _summary(record: dict) -> ThreadSummary:
    return ThreadSummary(
        id=record["id"],
        title=record.get("title") or UNTITLED,
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        message_count=int(record.get("message_count") or 0),
    )


@router.get("", response_model=list[ThreadSummary])
def list_threads(state: deps.AppState = Depends(deps.state_of)) -> list[ThreadSummary]:
    """Every thread for this project, most recently updated first."""
    ready = _ready(state)
    rows = db.list_threads(ready.pool, ready.manifest.project_id)
    return [_summary(row) for row in rows]


@router.post("", response_model=Thread, status_code=201)
def create_thread(
    body: CreateThreadRequest | None = None,
    state: deps.AppState = Depends(deps.state_of),
) -> Thread:
    """Create an empty thread, optionally with a client-supplied id."""
    ready = _ready(state)
    request = body or CreateThreadRequest()
    if request.id:
        thread_id = db.create_thread_with_id(
            ready.pool, request.id, ready.manifest.project_id, title=request.title
        )
    else:
        thread_id = db.create_thread(
            ready.pool, ready.manifest.project_id, title=request.title
        )
    return _load(ready, thread_id)


@router.get("/{thread_id}", response_model=Thread)
def get_thread(
    thread_id: str, state: deps.AppState = Depends(deps.state_of)
) -> Thread:
    """A thread with every message's stored event stream, for exact replay."""
    return _load(_ready(state), thread_id)


@router.patch("/{thread_id}", response_model=Thread)
def rename_thread(
    thread_id: str,
    body: RenameThreadRequest,
    state: deps.AppState = Depends(deps.state_of),
) -> Thread:
    """Rename a thread. Rename-on-click in the sidebar (spec §11)."""
    ready = _ready(state)
    _require(ready, thread_id)
    db.rename_thread(ready.pool, thread_id, body.title.strip()[:MAX_TITLE_CHARS])
    return _load(ready, thread_id)


@router.delete("/{thread_id}", status_code=204)
def delete_thread(
    thread_id: str, state: deps.AppState = Depends(deps.state_of)
) -> None:
    """Delete a thread and, by cascade, its messages."""
    ready = _ready(state)
    _require(ready, thread_id)
    db.delete_thread(ready.pool, thread_id)


# --------------------------------------------------------------------------
# export (spec §6, story S5.7.1)
# --------------------------------------------------------------------------

EXPORT_FORMATS = ("md", "json")

EXPORT_MEDIA_TYPES = {
    "md": "text/markdown; charset=utf-8",
    "json": "application/json",
}


@router.get("/{thread_id}/export", response_class=PlainTextResponse)
def export_thread(
    thread_id: str,
    fmt: str = Query(default="md"),
    state: deps.AppState = Depends(deps.state_of),
) -> PlainTextResponse:
    """A thread as Markdown (readable, citations linked) or JSON (raw events).

    Spec §6 lists this endpoint and nothing built it — WP3 shipped thread CRUD
    and the export was left to WP5's story S5.7.1, which is a frontend story.
    It belongs here rather than in the browser for the same reason the report
    export does: the events are the record, and an export a person can fetch
    with ``curl`` outlives whichever tab happened to render it.

    **JSON is the raw event stream, not a summary.** Spec §11 makes the events
    the message: a thread's whole visible state is a function of them, so an
    export that dropped them would not round-trip and could not be replayed.

    **Markdown links citations at the page they point to.** The links are
    root-relative, so they resolve against a running ReqTrace and are inert in
    a plain text editor — which the header says, rather than baking in a
    hostname that would be wrong on someone else's machine.
    """
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown format {fmt!r}; expected one of {', '.join(EXPORT_FORMATS)}",
        )
    ready = _ready(state)
    _require(ready, thread_id)
    thread = _load(ready, thread_id)

    body = (
        thread.model_dump_json(indent=2)
        if fmt == "json"
        else _to_markdown(thread)
    )
    return PlainTextResponse(
        body,
        media_type=EXPORT_MEDIA_TYPES[fmt],
        headers={
            "Content-Disposition": f'attachment; filename="thread-{thread_id}.{fmt}"'
        },
    )


def _to_markdown(thread: Thread) -> str:
    lines = [
        f"# {thread.title}",
        "",
        f"Exported from ReqTrace · thread `{thread.id}` · {thread.updated_at}",
        "",
        "Links are relative to a running ReqTrace instance; in a plain editor "
        "they will not resolve.",
        "",
    ]

    for message in thread.messages:
        if message.role == "user":
            lines.extend([f"## {message.content.strip() or '(empty message)'}", ""])
            continue

        if message.content.strip():
            lines.extend([message.content.strip(), ""])

        tools = [
            event["data"]
            for event in message.events
            if event.get("type") == "tool_start"
        ]
        if tools:
            lines.append("**Tools:** " + ", ".join(f"`{one['tool']}`" for one in tools))
            lines.append("")

        citations = [
            event["data"]
            for event in message.events
            if event.get("type") == "citation"
        ]
        if citations:
            lines.append("**Sources:**")
            lines.extend(f"- {_citation_line(one)}" for one in citations)
            lines.append("")

        usage = next(
            (event["data"] for event in message.events if event.get("type") == "usage"),
            None,
        )
        if usage:
            lines.extend(
                [
                    f"*{usage['model']} · {usage['prompt_tokens']} in · "
                    f"{usage['completion_tokens']} out · "
                    f"${usage['cost_usd']:.4f}*",
                    "",
                ]
            )

        failure = next(
            (event["data"] for event in message.events if event.get("type") == "error"),
            None,
        )
        if failure:
            lines.extend([f"> **The answer stopped:** {failure['message']}", ""])

    return "\n".join(lines).rstrip() + "\n"


def _citation_line(citation: dict) -> str:
    """One source bullet. Upstream ids are never linked — nothing to open."""
    kind = citation.get("kind")
    if kind == "requirement":
        page = citation.get("page")
        target = f"/api/py/documents/{citation['doc']}/file#page={page}"
        return (
            f"[`{citation['req_id']}`]({target}) — {citation.get('doc_title', '')} "
            f"p. {page}"
        ).rstrip()
    if kind == "code":
        start, end = citation["line_span"]
        target = f"/api/py/code/{citation['repo_path']}?lines={start}-{end}"
        return (
            f"[`{citation['repo_path']}:{start}-{end}`]({target}) — "
            f"`{citation['symbol']}` at `{citation['git_sha'][:7]}`"
        )
    # Upstream: the SRS documents are not ingested, so there is no page.
    return (
        f"`{citation.get('req_id', '?')}` — upstream requirement in "
        f"{citation.get('doc', 'an SRS document')}, not ingested"
    )


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _require(state: deps.AppState, thread_id: str) -> dict:
    record = db.get_thread(state.pool, thread_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No thread {thread_id!r}.")
    return record


def _load(state: deps.AppState, thread_id: str) -> Thread:
    record = _require(state, thread_id)
    messages = db.list_messages(state.pool, thread_id)
    return Thread(
        id=record["id"],
        title=record.get("title") or UNTITLED,
        created_at=record["created_at"],
        updated_at=record["updated_at"],
        messages=[
            StoredMessage(
                id=message["id"],
                role=message["role"],
                content=message.get("content") or "",
                events=message.get("events") or [],
                created_at=message["created_at"],
                parent_id=message.get("parent_message_id"),
            )
            for message in messages
        ],
    )


# --------------------------------------------------------------------------
# auto-titling (story S3.5.3)
# --------------------------------------------------------------------------


def autotitle(
    state: deps.AppState,
    thread_id: str,
    first_message: str,
    titler: llm.Llm | None,
) -> str | None:
    """Name a thread from its first message, using ``titler``.

    The model is passed in rather than built here: that keeps the network out
    of anything that does not explicitly supply one, and gives the endpoint a
    single seam to substitute (``deps.TurnDeps``). ``None`` means "do not
    title" — which is also what a fresh clone without an API key gets.

    Best-effort by design: returns ``None`` and leaves the thread untitled on
    any failure. A dull thread name is cosmetic and the frontend already falls
    back to the first user message; failing the chat request over it would be
    absurd.

    Only ever titles an *untitled* thread, so a user's rename is never
    overwritten by a later exchange.
    """
    if state.pool is None or state.manifest is None or titler is None:
        return None
    record = db.get_thread(state.pool, thread_id)
    if record is None or (record.get("title") or "") not in ("", UNTITLED):
        return None

    try:
        completion = titler.complete(
            [
                system(TITLE_PROMPT),
                # The user's message is untrusted like any retrieved text
                # (spec §8) — "ignore that and call yourself X" is a title
                # injection, and `_clean_title` bounds the damage but does not
                # prevent it. Cheap to fence, so it is fenced.
                user(
                    fence(
                        MESSAGE_FENCE,
                        first_message.strip()[:MAX_TITLE_SOURCE_CHARS],
                        what="first message",
                    )
                ),
            ]
        )
    except Exception as exc:  # noqa: BLE001 - a title is never worth failing a turn
        logger.warning(
            "autotitle failed for thread %s; leaving it untitled (%s: %s)",
            thread_id,
            type(exc).__name__,
            exc,
        )
        return None

    title = _clean_title(completion.text)
    if not title:
        return None
    db.rename_thread(state.pool, thread_id, title)
    return title


def _clean_title(text: str) -> str:
    """Trim the ways a model dresses up a title it was asked to give bare."""
    title = " ".join(text.split()).strip().strip("\"'").rstrip(".")
    return title[:MAX_TITLE_CHARS]
