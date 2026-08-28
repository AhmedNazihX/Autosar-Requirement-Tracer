"""The chat SSE wire format, and the per-turn cost meter (spec §6, story S3.1.1).

``frontend/lib/events.ts`` is the contract and says so in its own header: *"The
backend's ``POST /chat`` SSE envelope must match this file... do not fork
them."* This module is the Python side of that one contract. Every field name,
every optional marker and every discriminator here exists to match a line
there, and the tests assert the *serialised* shape rather than the Python
objects — a field renamed on one side of the proxy is invisible to a type check
on either side alone.

Three details are load-bearing and none of them is guessable from the plan:

**Optional means omitted, not null.** ``error?`` and ``stages?`` are optional
properties in TypeScript, so ``exclude_none=True`` is set on the envelope dump.
But ``bbox`` is *required and nullable* — the pane distinguishes "no rectangle
because the requirement crosses a page break" from "no bbox field at all" — so
it is explicitly excluded from that omission.

**One ``usage`` event per turn.** ``event-reducer.ts:120`` is ``usage =
event.data``: the last one wins and nothing accumulates. Emitting usage per
model call would report only whichever arrived last, so :class:`TurnMeter`
accumulates every purpose plus embeddings across the whole turn and produces a
single event at the end.

**A stream must end with ``done`` or ``error``.** The reducer labels a stream
with neither ``interrupted``, which would mislabel a complete answer.

The framing itself is deliberately conservative: one ``data:`` line per frame
holding compact JSON, terminated by a blank line. JSON escapes newlines, so a
token containing a blank line cannot terminate its own frame — which a
multi-line ``data:`` payload would risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from core.embeddings import EmbeddingUsage
from core.llm import LlmUsage

ToolName = Literal[
    "lookup_requirement",
    "search_requirements",
    "search_code",
    "check_implementation",
    "generate_traceability_report",
]

#: The five tools the agent may call, derived from the one definition above —
#: mirrored by ``events.ts`` ``TOOL_NAMES``, whose type guard drops anything
#: outside its union, so a sixth tool would vanish silently rather than fail
#: loudly. Keep the Python and TS lists equal; within Python, this tuple, the
#: injection eval and the ``TOOL_BUILDERS`` registry (guarded by a test) all
#: follow ``ToolName``.
TOOL_NAMES: tuple[str, ...] = get_args(ToolName)

#: The four verdicts of ``events.ts`` ``VERDICTS``. Consumed by WP4's judge;
#: defined here so both sides of the wire name them once.

#: Fields that are required-but-nullable on the wire. ``exclude_none`` would
#: drop them, and the frontend distinguishes ``null`` from absent: a ``bbox``
#: of ``null`` means "this requirement crosses a page break, so there is no
#: rectangle", while a missing ``bbox`` would be off-contract.
_NULLABLE_REQUIRED = {"bbox"}


class _Event(BaseModel):
    """Base for every event payload. ``extra="forbid"`` catches a typo'd field."""

    model_config = ConfigDict(extra="forbid")

    #: The ``type`` tag this payload travels under. Set per subclass.
    EVENT_TYPE: ClassVar[str]


class TokenEvent(_Event):
    """An incremental text delta for the assistant message."""

    EVENT_TYPE: ClassVar[str] = "token"

    text: str


class RagStage(BaseModel):
    """One RAG pipeline stage, as the tool chip shows it.

    Nested inside :class:`ToolResultEvent`, never sent on its own — so unlike
    the classes below it carries no ``type`` tag.
    """

    model_config = ConfigDict(extra="forbid")

    step: int = Field(ge=1)
    label: str
    detail: str


class ToolStartEvent(_Event):
    """A tool call has started. Renders as a pending chip."""

    EVENT_TYPE: ClassVar[str] = "tool_start"

    id: str = Field(min_length=1)
    tool: ToolName
    #: Verbatim tool arguments, rendered as data and never interpreted.
    args: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(_Event):
    """The outcome of a ``tool_start``, correlated by ``id``.

    The id must match a ``tool_start`` exactly: ``event-reducer.ts:103`` drops
    a result whose id it has not seen rather than inventing a chip for it.
    """

    EVENT_TYPE: ClassVar[str] = "tool_result"

    id: str = Field(min_length=1)
    status: Literal["ok", "error"]
    #: One short line for the chip, e.g. ``5 of 20`` or ``SWS_Can_00011``.
    summary: str
    duration_ms: int = Field(ge=0)
    #: Present only on ``status="error"``. User-readable, never a stack trace.
    error: str | None = None
    #: Only ``search_requirements`` reports these.
    stages: list[RagStage] | None = None
    #: Only ``generate_traceability_report`` reports this: the launched run's
    #: id, which the report drawer attaches to. Structured because prose is
    #: never parsed (spec §6).
    job_id: str | None = None


class RequirementCitation(_Event):
    """A structured pointer into a source document.

    ``doc`` is the manifest key (``can_driver``), never a filename — one
    identity end to end, from ``Requirement.source_doc`` through
    ``GET /documents/{doc}/view`` to the frontend.

    ``page_span`` is deliberately never populated: storage records only a
    requirement's *start* page (``core/models.py``), so there is no end page to
    send. ``events.ts:103`` documents this under ruling R21 — widen the model
    first rather than hunt for a field that was never stored.
    """

    EVENT_TYPE: ClassVar[str] = "citation"

    kind: Literal["requirement"] = "requirement"
    #: Exactly as the corpus spells it. Never normalised (finding B3).
    req_id: str
    doc: str
    doc_title: str
    page: int = Field(ge=1)
    bbox: tuple[float, float, float, float] | None
    page_count: int = Field(ge=0)
    page_span: tuple[int, int] | None = None
    section: str | None = None
    #: Verbatim extracted requirement text. Untrusted input.
    quote: str | None = None


class CodeCitation(_Event):
    """A structured pointer into the indexed code snapshot."""

    EVENT_TYPE: ClassVar[str] = "citation"

    kind: Literal["code"] = "code"
    repo_path: str
    symbol: str
    line_span: tuple[int, int]
    #: The pinned SHA the span is only true for.
    git_sha: str


class UpstreamCitation(_Event):
    """An upstream (SRS/RS) requirement cited by an SWS requirement.

    SRS documents are not ingested, so this renders as a dashed tooltipped chip
    and is never clickable through to a document (spec §11). It therefore
    carries no page and no bbox — there is nothing to open.
    """

    EVENT_TYPE: ClassVar[str] = "citation"

    kind: Literal["upstream"] = "upstream"
    req_id: str
    #: The SWS requirement that cites it.
    cited_by: str
    #: Display only — not a manifest key, since the document is not in the corpus.
    doc: str


class UsageEvent(_Event):
    """Tokens and cost for the whole message — see :class:`TurnMeter`."""

    EVENT_TYPE: ClassVar[str] = "usage"

    model: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0.0)
    elapsed_ms: int = Field(ge=0)


class DoneEvent(_Event):
    """Terminal success."""

    EVENT_TYPE: ClassVar[str] = "done"

    finish_reason: str | None = None


class ErrorEvent(_Event):
    """A user-readable failure. Renders inline and as a toast.

    Whatever streamed before the error is kept, so this must never be used to
    mean "discard the message".
    """

    EVENT_TYPE: ClassVar[str] = "error"

    message: str
    #: Machine-readable, for logs. Never shown raw to the user.
    code: str | None = None
    retryable: bool | None = None


ChatEvent = (
    TokenEvent
    | ToolStartEvent
    | ToolResultEvent
    | RequirementCitation
    | CodeCitation
    | UpstreamCitation
    | UsageEvent
    | DoneEvent
    | ErrorEvent
)


class ChatEventEnvelope(BaseModel):
    """The ``{type, data}`` wrapper every event travels in."""

    model_config = ConfigDict(extra="forbid")

    type: str
    data: dict[str, Any]

    @classmethod
    def of(cls, event: ChatEvent) -> ChatEventEnvelope:
        """Wrap ``event``, omitting absent optionals but keeping nullable ones.

        ``exclude_none=True`` is what makes ``error?`` and ``stages?`` absent
        rather than ``null``; the nullable-but-required fields are then put
        back, because the frontend reads ``bbox: null`` as a statement.
        """
        data = event.model_dump(mode="json", exclude_none=True)
        for name in _NULLABLE_REQUIRED:
            if name in type(event).model_fields and name not in data:
                data[name] = None
        return cls(type=event.EVENT_TYPE, data=data)


def sse_frame(event: ChatEvent) -> str:
    """``event`` as one SSE frame.

    A single ``data:`` line of compact JSON, terminated by the blank line
    ``chat-sources.ts`` splits on. JSON escapes newlines, so a token containing
    a blank line cannot terminate its own frame.
    """
    return f"data: {ChatEventEnvelope.of(event).model_dump_json()}\n\n"


# --------------------------------------------------------------------------
# the per-turn cost meter (story S3.1.1)
# --------------------------------------------------------------------------


@dataclass
class TurnMeter:
    """Accumulates every cost incurred answering one message.

    One turn touches several models — the chat model, the reranker, the query
    rewriter, the embedding model — and ``event-reducer.ts`` keeps only the last
    ``usage`` event it sees. So costs are accumulated here and emitted once, at
    the end of the turn, as a single total.

    The per-purpose breakdown is kept alongside because the total is what the
    user sees and the split is what a developer needs: "this answer cost
    $0.006" is the product feature, "of which the reranker was $0.0004" is how
    you find out why a search got expensive.
    """

    #: Names the turn on the wire. The chat model, since it is the one the user
    #: chose; the others are implementation detail of answering.
    model: str
    _llm: dict[str, LlmUsage] = field(default_factory=dict)
    _embedding: EmbeddingUsage = field(default_factory=EmbeddingUsage)
    #: Charges read straight off the HTTP responses by
    #: :mod:`core.usage_sniffer`. Separate from ``_llm`` because it covers the
    #: one case LangChain loses: a *streamed* reply's ``usage.cost``. Only the
    #: chat model's client sniffs — the judge, reranker and rewriter are
    #: non-streaming, so their cost arrives intact in ``LlmUsage`` and sniffing
    #: them too would double-count.
    _wire_cost_usd: float = 0.0

    def add_llm(self, usage: LlmUsage) -> None:
        """Accumulate one LLM call's usage under its own purpose."""
        existing = self._llm.get(usage.purpose)
        self._llm[usage.purpose] = existing.plus(usage) if existing else usage

    def add_embedding(self, usage: EmbeddingUsage) -> None:
        """Accumulate embedding usage, kept apart from the LLM purposes."""
        self._embedding = EmbeddingUsage(
            purpose=self._embedding.purpose,
            calls=self._embedding.calls + usage.calls,
            retries=self._embedding.retries + usage.retries,
            prompt_tokens=self._embedding.prompt_tokens + usage.prompt_tokens,
            total_tokens=self._embedding.total_tokens + usage.total_tokens,
            cost_usd=self._embedding.cost_usd + usage.cost_usd,
        )

    def add_pipeline(self, usage: Any) -> None:
        """Accumulate a whole :class:`~retrieval.pipeline.PipelineUsage`.

        Typed loosely to keep the wire module from importing the retrieval
        pipeline, which would make the API layer depend on it just to report a
        number.
        """
        for one in usage.llm:
            self.add_llm(one)
        self.add_embedding(usage.embedding)

    def add_wire_cost(self, cost_usd: float) -> None:
        """Add a charge observed on the wire (see :mod:`core.usage_sniffer`)."""
        if cost_usd > 0.0:
            self._wire_cost_usd += cost_usd

    def by_purpose(self) -> dict[str, LlmUsage]:
        """The LLM breakdown. Embeddings are not a purpose here."""
        return dict(self._llm)

    @property
    def cost_usd(self) -> float:
        return (
            sum(one.cost_usd for one in self._llm.values())
            + self._embedding.cost_usd
            + self._wire_cost_usd
        )

    def usage_event(self, *, elapsed_ms: int) -> UsageEvent:
        """The turn's single ``usage`` event."""
        return UsageEvent(
            model=self.model,
            prompt_tokens=(
                sum(one.prompt_tokens for one in self._llm.values())
                + self._embedding.prompt_tokens
            ),
            completion_tokens=sum(one.completion_tokens for one in self._llm.values()),
            cost_usd=self.cost_usd,
            elapsed_ms=elapsed_ms,
        )
