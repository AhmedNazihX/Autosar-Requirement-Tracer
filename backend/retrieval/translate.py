"""Multi-query expansion — RAG-Fusion's first stage (story S2.3.1).

The gap this closes is real and measurable on this corpus. A user asks "how
does the driver report bus-off?"; the requirement that answers it talks about
``CanIf_ControllerBusOff`` and the controller entering the ``BUSOFF`` state.
BM25 cannot bridge that — the two share barely a token — and dense retrieval
does the job better when the specification's own words are in the query rather
than only in the documents. So the question is rewritten three ways, each
rewrite retrieves independently, and Reciprocal Rank Fusion
(:mod:`retrieval.hybrid`) merges the results.

Two decisions shape everything here.

**The user's question is always query one.** Expansion may only ever *add*
candidates. If it could replace the user's words, a rewrite that drifted
off-topic would make the search worse than not expanding at all — and there is
no way to notice that from the outside.

**A failed expansion is not a failed search.** Query widening is an
optimisation; dying because an optimisation failed would be an absurd trade. If
the model errors, returns unparseable output, or returns nothing usable, the
stage returns the original question alone and records why in
:attr:`Expansion.fallback_reason`. The cost of the failed attempts is still
reported — it was still spent.

Everything the model is told about the corpus comes from the manifest
(:func:`retrieval.prompting.corpus_description`), so this module contains no
AUTOSAR knowledge of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from core.llm import Llm, LlmError, LlmUsage, system, user
from core.manifest import ProjectManifest
from retrieval.prompting import corpus_description, fence

#: Rewrites requested per question. Spec §4 says three. Each one costs a
#: retrieval round (and a query embedding), so this is a cost knob as much as a
#: quality one — story S7.2's evaluation is where changing it belongs.
DEFAULT_REWRITE_COUNT = 3

#: Longest question accepted (story S6.3.1's length cap, enforced here rather
#: than only at the HTTP edge). Generous for a question; far below a paste of a
#: whole document, which is what a caller sending 100kB is really doing.
MAX_QUERY_LENGTH = 2000

#: Longest rewrite kept. A rewrite is a search query; one longer than the
#: original question by this much is the model having ignored the task.
MAX_REWRITE_LENGTH = 400

#: Delimiter around the user's question. A constant so the tests — and story
#: S6.1.1's assertions — can check the fence is actually there.
QUESTION_FENCE = "<<<USER_QUESTION>>>"


class Rewrites(BaseModel):
    """The model's reply: alternative search queries, nothing else.

    ``min_length=1`` on purpose — an empty list is the model having declined
    the task, which is worth one retry with the error fed back
    (:meth:`core.llm.Llm.structured`) before falling back.
    """

    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(min_length=1, description="Alternative search queries.")


@dataclass(frozen=True)
class Expansion:
    """The queries to retrieve with, and what producing them cost.

    ``queries`` always starts with :attr:`original`. ``fallback_reason`` is
    ``None`` on the happy path and a human-readable sentence when the stage
    degraded — the pipeline log (story S2.6.1) surfaces it, so a silently
    unexpanded search is visible rather than merely slower.
    """

    original: str
    queries: list[str]
    usage: LlmUsage
    fallback_reason: str | None = None
    #: Rewrites the model produced that were dropped, and why — kept for the
    #: stage log rather than discarded, since "the model returned five and we
    #: used three" is exactly the kind of thing that confuses a RAGAS run.
    dropped: list[str] = field(default_factory=list)

    @property
    def rewrites(self) -> list[str]:
        """The queries the model added, without the original."""
        return self.queries[1:]

    @property
    def expanded(self) -> bool:
        return len(self.queries) > 1


SYSTEM_PROMPT = """\
You rewrite one engineering question into alternative search queries for a \
keyword-and-vector search over a technical specification corpus.

{corpus}

Rules:
- Produce exactly {count} rewrites, each a standalone search query.
- Bridge the vocabulary gap: prefer the specification's own terms — module \
prefixes, exact API and type names, defined state names, error constants — \
over the everyday words the question uses.
- Vary the angle between rewrites (the API involved, the state or event \
involved, the upper-layer effect). Near-identical rewrites waste a retrieval.
- Do NOT answer the question, explain anything, or add commentary.
- Return only the JSON object the schema describes.\
"""


def expand(
    llm: Llm,
    query: str,
    *,
    manifest: ProjectManifest,
    count: int = DEFAULT_REWRITE_COUNT,
) -> Expansion:
    """Rewrite ``query`` ``count`` ways, returning the original plus the rewrites.

    A pure function of its arguments: the model comes in, nothing is cached
    between calls, and the same canned reply always produces the same
    expansion. ``count=0`` makes no model call at all, which is the naive
    single-query baseline story S7.2 compares the full pipeline against.

    Raises ``ValueError`` for a blank or over-long question — that is a bad
    request, not something to degrade around.
    """
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("cannot expand an empty question")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(
            f"question is {len(query)} characters, longer than the "
            f"{MAX_QUERY_LENGTH} character limit"
        )
    if count < 0:
        raise ValueError(f"count must not be negative, got {count}")

    empty_usage = LlmUsage(purpose=llm.purpose)
    if count == 0:
        return Expansion(original=cleaned, queries=[cleaned], usage=empty_usage)

    messages = [
        system(SYSTEM_PROMPT.format(corpus=corpus_description(manifest), count=count)),
        user(fence(QUESTION_FENCE, cleaned, what="user question")),
    ]

    try:
        result = llm.structured(Rewrites, messages)
    except LlmError as exc:
        # Degrade, do not raise: the original question is still perfectly
        # searchable, and the caller's search should not fail because a
        # widening step did.
        return Expansion(
            original=cleaned,
            queries=[cleaned],
            usage=exc.usage if exc.usage is not None else empty_usage,
            fallback_reason=str(exc),
        )

    kept, dropped = _clean(result.value.queries, original=cleaned, count=count)
    if not kept:
        return Expansion(
            original=cleaned,
            queries=[cleaned],
            usage=result.usage,
            fallback_reason=(
                f"{llm.model_id} returned {len(result.value.queries)} rewrite(s), none usable"
            ),
            dropped=dropped,
        )
    return Expansion(
        original=cleaned,
        queries=[cleaned, *kept],
        usage=result.usage,
        dropped=dropped,
    )


def _clean(
    candidates: list[str], *, original: str, count: int
) -> tuple[list[str], list[str]]:
    """Drop the unusable rewrites and the duplicates; cap at ``count``.

    Comparison is case-folded and whitespace-collapsed, because a rewrite that
    merely re-cases the question would otherwise spend a retrieval round
    running the same search twice.
    """
    seen = {_key(original)}
    kept: list[str] = []
    dropped: list[str] = []
    for candidate in candidates:
        stripped = candidate.strip()
        key = _key(stripped)
        if not stripped or len(stripped) > MAX_REWRITE_LENGTH or key in seen:
            dropped.append(candidate)
            continue
        if len(kept) >= count:
            dropped.append(candidate)
            continue
        seen.add(key)
        kept.append(stripped)
    return kept, dropped


def _key(text: str) -> str:
    """A comparison key: case-folded, whitespace collapsed to single spaces."""
    return " ".join(text.split()).casefold()
