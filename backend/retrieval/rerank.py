"""LLM listwise reranking — the last stage of spec §4 (story S2.5.1).

Twenty fused candidates in, five out. Fusion (:mod:`retrieval.hybrid`) only
knows that two indexes agreed on something; this is the first stage that reads
the question and a candidate's text *together* and forms a judgement about
whether one answers the other.

**One ordering law does all the work.** The result is the model's chosen
positions first, then the remaining candidates in fusion order, truncated to
``top_n``. Every outcome is a case of that one rule:

* a complete answer is used as given;
* a partial answer — three of twenty named — is completed from fusion order,
  so the caller still gets the breadth it asked for;
* a total failure (transport error, unparseable reply, or an order naming only
  invalid positions) degenerates to pure fusion order, which is precisely the
  fallback story S2.5.1 requires.

There is deliberately no separate failure branch: the fallback is the same code
path with an empty set of model picks, so it cannot rot while the happy path
keeps working.

**Listwise, and by position.** The model is shown numbered candidates and
returns numbers, not ids. Ids here are chunk keys like
``code:communication/CanIf/src/CanIf.c:function:CanIf_Transmit:120-168`` —
asking a model to echo those exactly is asking for transcription errors that
are indistinguishable from a bad ranking, and it pays for the tokens twice.
Positions are one token each and an invalid one is unambiguously invalid.

**Candidate text is untrusted** (spec §8). It is retrieved document and code
text, so it is fenced and framed as data; the reranker is tool-less, so the
worst an injected instruction can achieve is a wrong ordering.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from core.llm import Llm, LlmError, LlmUsage, system, user
from retrieval.prompting import clip, fence

#: Results returned to the caller. Spec §4 says five.
DEFAULT_TOP_N = 5

#: Candidates the reranker is willing to be shown. Spec §4 fuses to twenty;
#: the cap exists so a caller cannot accidentally send the whole corpus and
#: turn one search into a dollar.
MAX_CANDIDATES = 40

#: Characters of each candidate shown to the model. Requirements average 283
#: characters and the longest is ~5000, so this keeps a full 20-candidate
#: prompt around 3-4k tokens rather than 15k, while still showing every normal
#: requirement in full.
MAX_CANDIDATE_CHARS = 1200

#: Delimiters, exposed so the tests — and story S6.1.1 — can assert the fences
#: are actually present rather than trusting the prompt string.
QUESTION_FENCE = "<<<USER_QUESTION>>>"
CANDIDATES_FENCE = "<<<RETRIEVED_CANDIDATES>>>"


@dataclass(frozen=True)
class Candidate:
    """One thing to be ranked: its chunk id, its text, and how to name it.

    ``label`` is what the model sees as the candidate's heading (``SWS_Can_00272
    — 7.4 Bus-off handling``). It helps: a requirement id and its section are
    often enough to judge relevance without reading the body.
    """

    id: str
    text: str
    label: str | None = None


class Ranking(BaseModel):
    """The model's reply: candidate positions, most relevant first."""

    model_config = ConfigDict(extra="forbid")

    order: list[int] = Field(
        min_length=1,
        description="Candidate numbers, most relevant first. Use the numbers in brackets.",
    )


@dataclass(frozen=True)
class Reranking:
    """The final order, what it cost, and how it was arrived at.

    ``completed_from_fusion`` counts how many of the returned ids the model did
    *not* choose. Zero means the model decided the whole result; a number equal
    to ``len(order)`` means the reranker contributed nothing and the caller is
    looking at pure fusion order. The pipeline log (story S2.6.1) reports it,
    because a reranker that silently stopped working would otherwise look
    exactly like one that agreed with fusion every time.
    """

    order: list[str]
    usage: LlmUsage
    fallback_reason: str | None = None
    completed_from_fusion: int = 0


SYSTEM_PROMPT = """\
You rank retrieved passages by how well each one answers a specific \
engineering question about a technical specification.

You will see a question and numbered candidate passages. Return the candidate \
numbers, most relevant first, best {top_n} only.

Judge whether the passage *answers the question*, not whether it merely shares \
vocabulary with it. A normative requirement that states the behaviour asked \
about outranks prose that mentions the same terms. A passage that is only a \
table of requirement identifiers answers nothing.

Return only the JSON object the schema describes — no explanation.\
"""


def rerank(
    llm: Llm,
    query: str,
    candidates: Sequence[Candidate],
    *,
    top_n: int = DEFAULT_TOP_N,
) -> Reranking:
    """Reorder ``candidates`` by relevance to ``query``, keeping the best ``top_n``.

    ``candidates`` must already be in fusion order — that order is both the
    tiebreak and the fallback. A pure function of its arguments; nothing is
    cached between calls.
    """
    if top_n < 1:
        raise ValueError(f"top_n must be at least 1, got {top_n}")
    if not query.strip():
        raise ValueError("cannot rerank against an empty question")

    fusion_order = [candidate.id for candidate in candidates]
    empty_usage = LlmUsage(purpose=llm.purpose)

    # Nothing to decide: no candidates, or only one. Paying a model to confirm
    # that a single candidate is the best of one would be pure waste.
    if len(candidates) < 2:
        return Reranking(
            order=fusion_order[:top_n],
            usage=empty_usage,
            completed_from_fusion=len(fusion_order[:top_n]),
        )

    shown = list(candidates[:MAX_CANDIDATES])
    messages = [
        system(SYSTEM_PROMPT.format(top_n=top_n)),
        user(
            f"{fence(QUESTION_FENCE, query.strip(), what='user question')}\n\n"
            f"{fence(CANDIDATES_FENCE, _render(shown), what='retrieved passages')}"
        ),
    ]

    try:
        result = llm.structured(Ranking, messages)
    except LlmError as exc:
        return _fallback(
            fusion_order,
            top_n,
            usage=exc.usage if exc.usage is not None else empty_usage,
            reason=str(exc),
        )

    picked = _valid_positions(result.value.order, len(shown))
    if not picked:
        return _fallback(
            fusion_order,
            top_n,
            usage=result.usage,
            reason=(
                f"{llm.model_id} returned {result.value.order!r}, no valid candidate position "
                f"in 1..{len(shown)}"
            ),
        )

    chosen = [shown[position - 1].id for position in picked]
    order = _complete(chosen, fusion_order, top_n)
    return Reranking(
        order=order,
        usage=result.usage,
        completed_from_fusion=len(order) - len(chosen[:top_n]),
    )


def _fallback(
    fusion_order: list[str], top_n: int, *, usage: LlmUsage, reason: str
) -> Reranking:
    """Pure fusion order — the ordering law with no model picks at all."""
    order = fusion_order[:top_n]
    return Reranking(
        order=order,
        usage=usage,
        fallback_reason=reason,
        completed_from_fusion=len(order),
    )


def _valid_positions(raw: Sequence[int], count: int) -> list[int]:
    """The in-range positions of ``raw``, first occurrence only.

    Out-of-range and repeated positions are dropped rather than treated as
    fatal: a model that names 21 candidates out of 20 has made one mistake in
    an otherwise usable ranking, and discarding the whole thing over it would
    throw away a call already paid for.
    """
    seen: set[int] = set()
    kept: list[int] = []
    for position in raw:
        if 1 <= position <= count and position not in seen:
            seen.add(position)
            kept.append(position)
    return kept


def _complete(chosen: list[str], fusion_order: list[str], top_n: int) -> list[str]:
    """The ordering law: model picks, then fusion order, truncated to ``top_n``."""
    order = list(chosen[:top_n])
    if len(order) < top_n:
        placed = set(order)
        for chunk_id in fusion_order:
            if len(order) >= top_n:
                break
            if chunk_id not in placed:
                order.append(chunk_id)
                placed.add(chunk_id)
    return order


def _render(candidates: Sequence[Candidate]) -> str:
    """Numbered candidates, each truncated to a sane length."""
    blocks = []
    for position, candidate in enumerate(candidates, start=1):
        heading = f"[{position}]"
        if candidate.label:
            heading = f"{heading} {candidate.label}"
        blocks.append(f"{heading}\n{clip(candidate.text, MAX_CANDIDATE_CHARS)}")
    return "\n\n".join(blocks)

