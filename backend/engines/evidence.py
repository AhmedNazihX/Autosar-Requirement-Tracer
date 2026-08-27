"""The three-tier evidence engine — ``check_implementation`` (spec §5, F4.1).

"Is this requirement implemented in the pinned snapshot?" answered cheapest
first, so the expensive tier only ever runs on material the cheap tiers found:

* **T1 — annotation scan** (story S4.1.1). The C sources carry ``@req``/``!req``
  comments naming AUTOSAR requirements. Canonicalised at ingestion, they are an
  exact index lookup here, and they cost nothing. They are *claims*, not
  findings — see below.
* **T2 — anchor retrieval** (story S4.1.2). The requirement's own
  ``named_symbols`` pull the matching ``CodeUnit``s by exact symbol lookup;
  hybrid search then adds semantic candidates for the case where the
  implementation exists under a name the specification never mentions.
* **T3 — LLM judge** (story S4.1.3). A tool-less structured call that reads the
  requirement and the candidates together and returns a Pydantic-validated
  :class:`Verdict`.

Five decisions here are load-bearing and none is obvious from the plan.

**An annotation is a claim, and the judge is told so.** ``@req CANIF023`` means
a developer asserted that this code implements that requirement, against an
*older AUTOSAR release* (finding A4: the 4.0.3 → R23-11 drift is the product's
subject). Trusting it would make the report a re-print of the source comments
rather than an analysis of them, and ``!req`` — 150 of them, finding A2 — would
be indistinguishable from silence. So both polarities are gathered, labelled,
and handed to T3 as evidence to weigh.

**The judge answers with candidate numbers, not file paths.** Asking a model to
echo ``communication/CanIf/src/CanIf.c`` and ``120-168`` exactly is asking for
transcription errors that look identical to a bad judgement, and it pays for
those tokens twice. So the model picks positions and this module maps them back
onto the real units — the same reasoning, for the same reason, as
:mod:`retrieval.rerank`.

**``unverifiable`` is a real answer and the failure mode.** Spec §5 refuses a
binary. Every path that cannot produce a judgement — a transport failure, two
attempts at invalid JSON — resolves to ``unverifiable`` with the reason in the
rationale, never to ``missing``: "I could not tell" and "it is not there" are
different statements about the code, and reporting the second when the first is
true is the one dishonesty this feature must not commit.

**Tier 2's semantic pass does not rerank.** ``search_code`` would spend an extra
LLM call per requirement narrowing 20 candidates to 5, and the judge then reads
all of them anyway. Over a 398-requirement report that is a second model call
per row buying nothing, so fusion order is taken as-is.

**Nothing here raises for a corpus reason.** An unknown id, no candidates, a
model that will not answer — each is a returned verdict, because this runs
398 times inside a batch job and one bad row must not end the run.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from core import db
from core.llm import Llm, LlmError, LlmUsage, system, user
from core.models import AnnotationClaim, CodeUnit, Requirement
from retrieval import pipeline
from retrieval.lookup import InvalidRequirementId, lookup
from retrieval.pipeline import Engine, PipelineUsage, StageLog
from retrieval.prompting import fence

#: Candidates the judge is shown. Spec §5 says "≤8 total"; the budget is spent
#: annotations first, then anchors, then semantic fill.
MAX_CANDIDATES = 8

#: Semantic candidates asked of ``search_code``. Fusion order, no rerank — see
#: the module docstring.
SEMANTIC_TOP_N = 5

#: Characters of a candidate's source shown to the judge. A CAN function runs
#: to a few hundred lines; the whole of one is rarely needed to see whether it
#: implements a requirement, and eight full functions would be a 20k-token
#: prompt on every row of a 398-row report.
MAX_CANDIDATE_CHARS = 2400

TRUNCATION_MARKER = "\n…[truncated]"

#: Delimiters, named so story S6.1.1 can assert the fences exist rather than
#: trust the prompt string.
REQUIREMENT_FENCE = "<<<REQUIREMENT_TEXT>>>"
CANDIDATES_FENCE = "<<<CANDIDATE_SOURCE>>>"

#: How a candidate came to be in front of the judge. Shown to it, because
#: "the developers annotated this" and "this looked similar" are very
#: different grounds for believing a candidate is relevant.
FoundBy = str
BY_ANNOTATION: FoundBy = "annotation"
BY_SYMBOL: FoundBy = "symbol"
BY_SEMANTIC: FoundBy = "semantic"

#: What an annotation-found candidate is called in ``blind`` mode: the fact
#: that a comment names the requirement is kept, its claim is not.
BY_REFERENCE: FoundBy = "reference"

#: Stands in for a redacted annotation, so the judge sees that something was
#: withheld rather than reading a comment with a hole in it.
REDACTED_MARKER = "[requirement annotation withheld]"

#: Definitions before declarations. A header prototype is not an
#: implementation, so an annotated prototype must never outrank an annotated
#: definition in the candidate list (the same priority
#: ``retrieval.pipeline`` applies to exact symbol hits).
_KIND_PRIORITY = {"function": 0}

_CLAIM_LABEL: dict[AnnotationClaim, str] = {
    "claimed_implemented": "@req — the developers claim this code implements it",
    "claimed_not_implemented": "!req — the developers state this is NOT implemented here",
}


# --------------------------------------------------------------------------
# what a check produces
# --------------------------------------------------------------------------


class EvidenceItem(BaseModel):
    """One piece of code the judge cited, resolved back to real coordinates.

    ``file``/``lines``/``rationale`` are spec §5's payload verbatim.
    ``symbol`` and ``git_sha`` ride along because a ``code`` citation event
    (``frontend/lib/events.ts``) needs them and re-deriving them from a path
    and a line span would be a second lookup per row.
    """

    model_config = ConfigDict(extra="forbid")

    file: str
    lines: tuple[int, int]
    rationale: str
    symbol: str
    git_sha: str


class Verdict(BaseModel):
    """The evidence engine's answer about one requirement.

    Persisted verbatim into ``verdict_cache`` (story S4.2.1), so it is a
    Pydantic model rather than a dataclass: the cache round-trips it through
    JSON, and a field added later must not silently read back as absent.
    """

    model_config = ConfigDict(extra="forbid")

    status: str = Field(pattern="^(implemented|partial|missing|unverifiable)$")
    evidence: list[EvidenceItem] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    #: One sentence a human can read without opening the code.
    rationale: str = ""
    #: The judge model this verdict came from, or ``None`` when no model was
    #: called. Recorded because a cached verdict must be able to say what
    #: produced it — the cache key holds the same id, but an exported report
    #: is read far away from the cache.
    model_id: str | None = None


@dataclass(frozen=True)
class Candidate:
    """One code unit put in front of the judge, and why it is there."""

    unit: CodeUnit
    found_by: FoundBy
    #: Set only for ``found_by="annotation"``: what the annotation claimed.
    claim: AnnotationClaim | None = None
    #: Set only for ``found_by="annotation"``: the line the annotation sits on.
    annotation_lines: tuple[int, ...] = ()


@dataclass(frozen=True)
class EvidenceCheck:
    """A finished check: the verdict, what it saw, and what it cost."""

    req_id: str
    requirement: Requirement | None
    verdict: Verdict
    candidates: tuple[Candidate, ...] = ()
    usage: PipelineUsage = field(default_factory=PipelineUsage)
    #: LLM usage of the judge call itself, kept out of ``usage`` because
    #: ``PipelineUsage`` is the *retrieval* pipeline's accounting.
    judge_usage: LlmUsage | None = None
    stages: tuple[StageLog, ...] = ()
    #: True when the verdict came from ``verdict_cache`` and nothing was spent.
    cached: bool = False

    @property
    def cost_usd(self) -> float:
        return self.usage.cost_usd + (self.judge_usage.cost_usd if self.judge_usage else 0.0)


# --------------------------------------------------------------------------
# T1 — annotation scan (story S4.1.1)
# --------------------------------------------------------------------------


def annotation_candidates(engine: Engine, requirement: Requirement) -> list[Candidate]:
    """Claimed evidence for ``requirement``, with file and line, both polarities.

    Ordered definitions-first: an ``@req`` on a header prototype is a claim
    about an API, not about an implementation, and the judge should meet the
    stronger candidate before it runs out of context.
    """
    canonical = Requirement.canonical_id(requirement.id)
    units = db.list_code_units_by_annotation(engine.conn, engine.project_id, canonical)

    candidates: list[Candidate] = []
    for unit in units:
        matching = [
            annotation
            for annotation in unit.req_annotations
            if Requirement.canonical_id(annotation.canonical_id) == canonical
        ]
        if not matching:  # pragma: no cover - the query selected on this
            continue
        # A unit whose comment block both claims and denies the same id is
        # contradicting itself. Report the denial: it is the specific
        # statement, and treating the pair as a claim would silently lose it.
        claim: AnnotationClaim = (
            "claimed_not_implemented"
            if any(a.claim == "claimed_not_implemented" for a in matching)
            else "claimed_implemented"
        )
        candidates.append(
            Candidate(
                unit=unit,
                found_by=BY_ANNOTATION,
                claim=claim,
                annotation_lines=tuple(sorted(a.line for a in matching)),
            )
        )
    return sorted(candidates, key=lambda c: _unit_order(c.unit))


# --------------------------------------------------------------------------
# T2 — anchor retrieval (story S4.1.2)
# --------------------------------------------------------------------------


def anchor_candidates(engine: Engine, requirement: Requirement) -> list[Candidate]:
    """Exact ``named_symbols`` lookups — the free half of tier 2.

    60–62% of requirements name a C symbol (finding B5), and when they do it
    is the single most reliable pointer into the code there is: no embedding,
    no ranking, no model.
    """
    candidates: list[Candidate] = []
    for symbol in requirement.named_symbols:
        for unit in db.list_code_units_by_symbol(engine.conn, engine.project_id, symbol.strip()):
            candidates.append(Candidate(unit=unit, found_by=BY_SYMBOL))
    return sorted(candidates, key=lambda c: _unit_order(c.unit))


def semantic_candidates(
    engine: Engine, requirement: Requirement, *, top_n: int = SEMANTIC_TOP_N
) -> tuple[list[Candidate], PipelineUsage, tuple[StageLog, ...]]:
    """Hybrid search over the code index — the paid half of tier 2.

    Catches the implementation that exists under a name the specification
    never uses, which exact anchoring cannot. Never raises: a search that
    fails leaves the judge with the cheap tiers, which is a worse answer but
    still an answer.
    """
    query = (requirement.title or requirement.text).strip()
    if not query:  # pragma: no cover - Requirement.text has min_length=1
        return [], PipelineUsage(), ()
    try:
        result = pipeline.search_code(engine, query=query, top_n=top_n, use_rerank=False)
    except Exception:  # noqa: BLE001 - one row must not end a 398-row report
        return [], PipelineUsage(), ()
    return (
        [Candidate(unit=item.unit, found_by=BY_SEMANTIC) for item in result.results],
        result.usage,
        tuple(result.stages),
    )


def gather_candidates(
    engine: Engine,
    requirement: Requirement,
    *,
    limit: int = MAX_CANDIDATES,
    include_semantic: bool = True,
) -> tuple[list[Candidate], PipelineUsage, tuple[StageLog, ...]]:
    """Tiers 1 and 2 merged into the ≤8 candidates spec §5 allows.

    Order is the budget: annotations, then exact anchors, then semantic fill.
    A unit reached by two tiers is kept once, under the *first* tier that
    found it — the annotation is the more informative label, and showing the
    same function twice would waste a slot and imply two pieces of evidence.

    The semantic pass is skipped when the cheap tiers already fill the budget,
    which is what makes an annotated requirement cost nothing before the judge.

    ``include_semantic=False`` stops before it unconditionally, which is how
    the report's cost estimate (:func:`engines.report.estimate_cost`) counts
    real candidates without paying for any retrieval — it must not spend money
    working out what spending money would cost.
    """
    candidates: list[Candidate] = []
    seen: set[tuple[str, str, str, tuple[int, int]]] = set()

    def take(items: Sequence[Candidate]) -> None:
        for candidate in items:
            key = _identity(candidate.unit)
            if key in seen or len(candidates) >= limit:
                continue
            seen.add(key)
            candidates.append(candidate)

    take(annotation_candidates(engine, requirement))
    take(anchor_candidates(engine, requirement))
    if not include_semantic or len(candidates) >= limit:
        return candidates, PipelineUsage(), ()

    semantic, usage, stages = semantic_candidates(engine, requirement)
    take(semantic)
    return candidates, usage, stages


# --------------------------------------------------------------------------
# T3 — the judge (story S4.1.3)
# --------------------------------------------------------------------------


class _JudgeEvidence(BaseModel):
    """One citation in the model's reply: a candidate number and why."""

    model_config = ConfigDict(extra="forbid")

    candidate: int = Field(ge=1, description="The candidate's number in brackets.")
    rationale: str = Field(
        min_length=1, description="One sentence: what this code does about the requirement."
    )


class JudgeReply(BaseModel):
    """The judge's structured answer. Positions, never file paths."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(
        pattern="^(implemented|partial|missing|unverifiable)$",
        description=(
            "implemented: the candidates fully satisfy the requirement. "
            "partial: some of the required behaviour is present. "
            "missing: none of the candidates implements it. "
            "unverifiable: the candidates do not let you decide."
        ),
    )
    evidence: list[_JudgeEvidence] = Field(
        default_factory=list,
        description="Candidates that support the status. Empty for missing.",
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="How sure you are, 0.0 to 1.0."
    )
    rationale: str = Field(
        min_length=1, description="One sentence explaining the status, for a human reader."
    )


SYSTEM_PROMPT = """\
You decide whether a specification requirement is implemented by a snapshot of \
C source code, and you report what the code shows rather than what anyone \
claims about it.

You will see one requirement and up to {max_candidates} numbered candidate code \
units. Return one of four statuses:

- implemented — a candidate clearly performs what the requirement mandates.
- partial — some of the mandated behaviour is present, or it is present only \
for some of the cases the requirement covers.
- missing — the candidates are relevant enough to judge, and none of them \
implements the requirement.
- unverifiable — you cannot tell from what you were shown: the candidates are \
unrelated, or the requirement is about configuration, documentation or \
naming that source code cannot settle.

`unverifiable` is a correct and expected answer. Never guess `missing` when \
you mean "I cannot tell"; they are different statements about this code.

Weigh the candidates by how each was found:

- annotation — a developer comment naming this requirement. `@req` claims it \
IS implemented, `!req` states it is NOT. These are claims about an OLDER \
release of the standard, written by the original authors and never verified. \
Treat them as strong hints and check them against the code you were shown; an \
`@req` on code that plainly does something else does not make the requirement \
implemented, and an `!req` beside code that plainly does implement it does not \
make it missing.
- symbol — the requirement names this function by name. Strong relevance.
- semantic — retrieved because it reads similarly. It may be unrelated, and \
often is; a candidate found this way alone rarely supports `implemented`.

A prototype or a declaration is not an implementation. A macro or a type \
definition satisfies a requirement only when the requirement is about that \
declaration.

This snapshot implements an older release of the standard than the \
specification does, so a requirement with no implementation is an ordinary and \
expected outcome, not a defect to explain away.

Cite evidence by candidate NUMBER. Return only the JSON object the schema \
describes.\
"""

#: The same task with the annotation channel closed — used only by story
#: S4.4.2's blind evaluation. It is a separate string rather than a
#: conditional inside :data:`SYSTEM_PROMPT` so that the product prompt cannot
#: be changed by accident while editing the measurement one, and so that a
#: diff shows exactly what the judge was and was not told.
BLIND_SYSTEM_PROMPT = """\
You decide whether a specification requirement is implemented by a snapshot of \
C source code, reading only the code.

You will see one requirement and up to {max_candidates} numbered candidate code \
units. Return one of four statuses:

- implemented — a candidate clearly performs what the requirement mandates.
- partial — some of the mandated behaviour is present, or it is present only \
for some of the cases the requirement covers.
- missing — the candidates are relevant enough to judge, and none of them \
implements the requirement.
- unverifiable — you cannot tell from what you were shown: the candidates are \
unrelated, or the requirement is about configuration, documentation or \
naming that source code cannot settle.

`unverifiable` is a correct and expected answer. Never guess `missing` when \
you mean "I cannot tell"; they are different statements about this code.

Weigh the candidates by how each was found:

- reference — a developer comment in this unit names this requirement. \
Whether that comment claims the requirement implemented or explicitly \
unimplemented has been withheld from you deliberately, and the marker is \
redacted from the source. Decide from the code itself.
- symbol — the requirement names this function by name. Strong relevance.
- semantic — retrieved because it reads similarly. It may be unrelated, and \
often is; a candidate found this way alone rarely supports `implemented`.

A prototype or a declaration is not an implementation. A macro or a type \
definition satisfies a requirement only when the requirement is about that \
declaration.

This snapshot implements an older release of the standard than the \
specification does, so a requirement with no implementation is an ordinary and \
expected outcome, not a defect to explain away.

Cite evidence by candidate NUMBER. Return only the JSON object the schema \
describes.\
"""


def judge(
    llm: Llm,
    requirement: Requirement,
    candidates: Sequence[Candidate],
    *,
    max_candidate_chars: int = MAX_CANDIDATE_CHARS,
    blind: bool = False,
) -> tuple[Verdict, LlmUsage]:
    """Tier 3: one tool-less structured call, resolved back onto real code.

    ``llm`` must be tool-less (spec §8): retrieved source is untrusted input,
    it is fenced as data here, and the worst an injected instruction can then
    achieve is one wrong verdict rather than an action.

    ``blind`` withholds every trace of the ``@req``/``!req`` annotations — the
    per-candidate claim line *and* the marker text inside the source, since a
    unit's span starts at its leading comment block
    (``ingestion/code_indexer.py``) and therefore contains the annotation
    verbatim. It exists for story S4.4.2 and only for it: those annotations
    are that evaluation's ground truth, so a judge that can read them is being
    handed the answer, and the resulting accuracy would measure obedience
    rather than analysis. Never set it in the product path — the annotations
    are real evidence and withholding them would make every verdict worse.
    """
    empty_usage = LlmUsage(purpose=llm.purpose)

    if not candidates:
        # Nothing at all names, is named by, or resembles this requirement.
        # That is a finding, and paying a model to look at an empty list is not
        # a way of confirming it.
        return (
            Verdict(
                status="missing",
                confidence=0.6,
                rationale=(
                    "No code unit in the pinned snapshot annotates this requirement, "
                    "is named by it, or resembles it."
                ),
            ),
            empty_usage,
        )

    shown = list(candidates)
    prompt = BLIND_SYSTEM_PROMPT if blind else SYSTEM_PROMPT
    rendered = _render(shown, max_candidate_chars, blind=blind)
    messages = [
        system(prompt.format(max_candidates=len(shown))),
        user(
            f"{fence(REQUIREMENT_FENCE, _requirement_block(requirement), what='requirement')}"
            f"\n\n"
            f"{fence(CANDIDATES_FENCE, rendered, what='C source')}"
        ),
    ]

    try:
        result = llm.structured(JudgeReply, messages)
    except LlmError as exc:
        # Two attempts at valid JSON have already happened inside `structured`
        # (the second with the validation error fed back). Story S4.1.3 makes
        # the outcome `unverifiable`, never `missing`.
        return (
            Verdict(
                status="unverifiable",
                confidence=0.0,
                rationale=f"The judge model did not return a usable verdict: {exc}",
                model_id=llm.model_id,
            ),
            exc.usage if exc.usage is not None else empty_usage,
        )

    reply = result.value
    return (
        Verdict(
            status=reply.status,
            evidence=_resolve(reply.evidence, shown),
            confidence=reply.confidence,
            rationale=reply.rationale,
            model_id=llm.model_id,
        ),
        result.usage,
    )


# --------------------------------------------------------------------------
# the whole check
# --------------------------------------------------------------------------


def check_implementation(
    engine: Engine,
    judge_llm: Llm,
    req_id: str,
    *,
    use_cache: bool = True,
    limit: int = MAX_CANDIDATES,
    blind: bool = False,
) -> EvidenceCheck:
    """All three tiers for one requirement, cache first.

    **The cache is consulted before tier 2, not before the judge.** Tier 2's
    semantic pass embeds a query and searches two indexes; short-circuiting
    only the model call would leave a "cached" re-run still paying for
    retrieval, and story S4.2.1's acceptance criterion is that an unchanged
    re-run makes *no* calls at all.

    The key is spec §5's, exactly: ``(req_id, git_sha, model_id)``. A new
    snapshot or a re-pinned judge model therefore re-judges, which is the
    invalidation the same story requires.
    """
    started = time.perf_counter()
    try:
        requirement = lookup(engine.conn, engine.project_id, req_id)
    except InvalidRequirementId as exc:
        return EvidenceCheck(
            req_id=req_id,
            requirement=None,
            verdict=Verdict(
                status="unverifiable",
                confidence=0.0,
                rationale=f"{req_id!r} is not a requirement id: {exc}",
            ),
        )
    if requirement is None:
        return EvidenceCheck(
            req_id=req_id,
            requirement=None,
            verdict=Verdict(
                status="unverifiable",
                confidence=0.0,
                rationale=(
                    f"There is no requirement {req_id!r} in this corpus, so there is "
                    "nothing to check it against."
                ),
            ),
        )

    git_sha = engine.manifest.code.git_sha

    cache_model_id = f"{judge_llm.model_id}#blind" if blind else judge_llm.model_id
    if use_cache:
        cached = db.get_verdict(
            engine.conn, Requirement.canonical_id(requirement.id), git_sha, cache_model_id
        )
        if cached is not None:
            return EvidenceCheck(
                req_id=requirement.id,
                requirement=requirement,
                verdict=Verdict.model_validate(cached),
                cached=True,
            )

    candidates, usage, stages = gather_candidates(engine, requirement, limit=limit)
    verdict, judge_usage = judge(judge_llm, requirement, candidates, blind=blind)

    db.put_verdict(
        engine.conn,
        Requirement.canonical_id(requirement.id),
        git_sha,
        cache_model_id,
        verdict.model_dump(mode="json"),
        engine.project_id,
    )

    stages = (
        *stages,
        StageLog(
            name="judge",
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            cost_usd=judge_usage.cost_usd,
            detail={"candidates": len(candidates), "status": verdict.status},
        ),
    )
    return EvidenceCheck(
        req_id=requirement.id,
        requirement=requirement,
        verdict=verdict,
        candidates=tuple(candidates),
        usage=usage,
        judge_usage=judge_usage,
        stages=stages,
    )


# --------------------------------------------------------------------------
# rendering and small helpers
# --------------------------------------------------------------------------


def _requirement_block(requirement: Requirement) -> str:
    """The requirement as the judge sees it: id, where it lives, its text."""
    lines = [f"id: {requirement.id}"]
    if requirement.title:
        lines.append(f"title: {requirement.title}")
    if requirement.section_path:
        lines.append(f"section: {requirement.section_path}")
    if requirement.named_symbols:
        lines.append(f"names the C symbols: {', '.join(requirement.named_symbols)}")
    lines.append("")
    lines.append(requirement.text.strip())
    return "\n".join(lines)


def _render(candidates: Sequence[Candidate], max_chars: int, *, blind: bool = False) -> str:
    """Numbered candidates, each headed by how it was found."""
    blocks = []
    for position, candidate in enumerate(candidates, start=1):
        unit = candidate.unit
        start, end = unit.line_span
        found_by = BY_REFERENCE if (blind and candidate.claim is not None) else candidate.found_by
        head = (
            f"[{position}] {unit.repo_path}:{start}-{end} "
            f"({unit.kind} {unit.symbol}) — found by {found_by}"
        )
        if candidate.claim is not None and not blind:
            lines = ", ".join(str(line) for line in candidate.annotation_lines)
            head += f"\n    {_CLAIM_LABEL[candidate.claim]} (line {lines})"
        text = redact_annotations(unit) if blind else unit.text
        blocks.append(f"{head}\n{_clip(text, max_chars)}")
    return "\n\n".join(blocks)


def redact_annotations(unit: CodeUnit) -> str:
    """``unit.text`` with every ``@req``/``!req`` marker removed.

    A code unit's span begins at its attached comment block
    (``ingestion/code_indexer.py``), so the annotation text is *inside* the
    source the judge reads. Hiding only the per-candidate label would blind
    the header and leave the answer in the body.

    ``ReqAnnotation.raw`` is the matched text verbatim, so the removal is
    exact rather than a second regex that could drift from the manifest's.
    """
    text = unit.text
    for annotation in unit.req_annotations:
        text = text.replace(annotation.raw, REDACTED_MARKER)
    return text


def _clip(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars] + TRUNCATION_MARKER


def _resolve(
    evidence: Sequence[_JudgeEvidence], candidates: Sequence[Candidate]
) -> list[EvidenceItem]:
    """Map candidate numbers back onto real files, dropping invalid ones.

    An out-of-range or repeated number is one mistake in an otherwise usable
    verdict, so it is dropped rather than treated as fatal — the same rule
    :func:`retrieval.rerank._valid_positions` applies for the same reason.
    """
    resolved: list[EvidenceItem] = []
    used: set[int] = set()
    for item in evidence:
        position = item.candidate
        if not 1 <= position <= len(candidates) or position in used:
            continue
        used.add(position)
        unit = candidates[position - 1].unit
        resolved.append(
            EvidenceItem(
                file=unit.repo_path,
                lines=unit.line_span,
                rationale=item.rationale.strip(),
                symbol=unit.symbol,
                git_sha=unit.git_sha,
            )
        )
    return resolved


def _identity(unit: CodeUnit) -> tuple[str, str, str, tuple[int, int]]:
    """A code unit's primary key — symbols are not unique in a file (C5)."""
    return (unit.repo_path, unit.kind, unit.symbol, unit.line_span)


def _unit_order(unit: CodeUnit) -> tuple[int, str, int]:
    return (_KIND_PRIORITY.get(unit.kind, 1), unit.repo_path, unit.line_span[0])
