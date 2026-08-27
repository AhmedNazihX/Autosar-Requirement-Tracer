"""Self-query structured filters (story S2.4.1).

Spec §4's second stage: an LLM reads the question and extracts a *typed* filter
— module, section, chunk kind — which is then applied as a metadata constraint
so the search is narrowed before it runs rather than after. "Which scheduled
functions does the CAN driver have?" becomes ``module=Can``,
``section=Scheduled functions``, ``doc_type=requirement``, and the ANN search
never looks at CanTp at all.

**The rule this module is built around: a filter the corpus cannot satisfy is
dropped, never applied.** An LLM answering ``module="CAN Driver"`` (the
document's title, not the module name) or ``section="Error Handling"`` (a
section this corpus does not have) is completely ordinary. Applying either as a
hard constraint returns zero hits — and zero hits reads to the user as "nothing
in the specification covers this", which is a *wrong answer*, not a narrow one.
An unfiltered search is merely less precise. So every field is resolved against
what the corpus actually contains, and whatever will not resolve is recorded in
:attr:`Filter.dropped` and left off the query.

**Both indexes honour the same filter.** BM25 is built from every row in
SQLite; Chroma holds a subset. Filtering only the vector side would let the
fused result carry lexical hits that violate the user's constraint, so
:meth:`Filter.where` serves Chroma and :meth:`Filter.matches` serves BM25 from
the same resolved values.

**Sections are resolved, not matched.** Chroma's ``where`` has no substring
operator, so a section *hint* is turned into the set of real ``section_path``
values containing it and applied as ``$in``. That also makes the hint's effect
inspectable: 522 distinct section paths exist, and knowing which ones a hint
selected is the difference between a debuggable filter and a magic one.

As everywhere else in WP2, a model failure degrades — to no filter, with the
reason recorded — rather than failing the search.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.llm import Llm, LlmError, LlmUsage, system, user
from core.manifest import ProjectManifest
from retrieval.bm25 import Bm25Record
from retrieval.chunks import CODE_DOC_TYPE
from retrieval.prompting import corpus_description, fence

#: The chunk kinds a filter may name. Closed, so the model cannot invent a
#: fourth: ``requirement`` and ``context`` are ``Requirement.doc_type``'s two
#: values and ``code`` is what code chunks carry (:mod:`retrieval.chunks`).
DocTypeFilter = Literal["requirement", "context", "code"]

#: Longest section hint accepted. A hint is a few words from a heading; a
#: paragraph-length one is the model having misunderstood the task.
MAX_SECTION_LENGTH = 120

#: Most section paths one hint may resolve to. A hint matching half the corpus
#: is not a filter — applying it costs a large ``$in`` clause and narrows
#: nothing, so it is dropped like any other unusable field.
MAX_RESOLVED_SECTIONS = 40

#: Delimiter around the user's question, so story S6.1.1 can assert the fence.
QUESTION_FENCE = "<<<USER_QUESTION>>>"


def combine(conditions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Several Chroma conditions as one ``where`` clause.

    A single condition is emitted bare and several are wrapped in ``$and``,
    because Chroma rejects a one-element ``$and``.
    """
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


class QueryFilter(BaseModel):
    """The filter as the model returns it — unvalidated against the corpus.

    Every field is optional and ``None`` is the common case: most questions
    carry no constraint, and inventing one narrows the search wrongly.
    """

    model_config = ConfigDict(extra="forbid")

    module: str | None = Field(
        default=None,
        description="Module the question is about, e.g. Can, CanIf. Null if not stated.",
    )
    doc_type: DocTypeFilter | None = Field(
        default=None,
        description=(
            "'requirement' for normative shall-statements, 'context' for explanatory "
            "prose and definitions, 'code' for source. Null if the question does not imply one."
        ),
    )
    section: str | None = Field(
        default=None,
        description=(
            "Words from the document section heading the answer would live under, "
            "e.g. 'Scheduled functions'. Null if not implied."
        ),
    )

    @property
    def is_empty(self) -> bool:
        return self.module is None and self.doc_type is None and self.section is None


@dataclass(frozen=True)
class Filter:
    """A filter checked against the corpus and ready to apply.

    ``dropped`` pairs a field name with why it could not be used. It is kept
    rather than logged because "the filter was silently discarded" and "the
    filter matched nothing" look identical from the outside, and the pipeline
    log (story S2.6.1) needs to tell them apart.
    """

    module: str | None = None
    doc_type: str | None = None
    section_paths: tuple[str, ...] = ()
    dropped: tuple[tuple[str, str], ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.module is None and self.doc_type is None and not self.section_paths

    def conditions(self) -> list[dict[str, Any]]:
        """This filter as a list of Chroma conditions, in a stable order.

        Exposed separately from :meth:`where` so the pipeline can add its own
        constraint — ``search_requirements`` excludes code chunks, and combining
        by nesting one ``where`` clause inside another is how a malformed
        ``$and`` gets built.
        """
        conditions: list[dict[str, Any]] = []
        if self.doc_type is not None:
            conditions.append({"doc_type": self.doc_type})
        if self.module is not None:
            conditions.append({"module": self.module})
        if self.section_paths:
            conditions.append({"section_path": {"$in": list(self.section_paths)}})
        return conditions

    def where(self) -> dict[str, Any] | None:
        """This filter as a Chroma ``where`` clause, or ``None`` if empty."""
        return combine(self.conditions())

    def matches(self, record: Bm25Record) -> bool:
        """Whether ``record`` satisfies this filter — the BM25 side of the same test."""
        if self.doc_type is not None and record.doc_type != self.doc_type:
            return False
        if self.module is not None and record.module != self.module:
            return False
        if self.section_paths and record.section_path not in self.section_paths:
            return False
        return True


# --------------------------------------------------------------------------
# resolving against the corpus
# --------------------------------------------------------------------------


def known_modules(manifest: ProjectManifest) -> list[str]:
    """Module names a filter may name, in the spelling the metadata uses.

    Taken from the manifest's document entries — the same source
    :func:`retrieval.chunks.module_by_document` tags chunks from, so a
    resolved module always matches something.
    """
    seen: list[str] = []
    for document in manifest.documents:
        if document.module not in seen:
            seen.append(document.module)
    return seen


def question_names_module(question: str, module: str, manifest: ProjectManifest) -> bool:
    """Does ``question`` actually name ``module``?

    The guard on an *inferred* module filter, added because the F7.2 RAGAS run
    measured what happens without it: the extractor read "controller **state**"
    as the state manager and "E_TX_ON **effect**" as the driver, and each wrong
    filter excluded the document holding the answer. Two of the full pipeline's
    three misses on the golden set were this, and nothing downstream can
    recover from it — the right requirement is not in the candidate set to
    rerank.

    Three ways a question names a module, in decreasing certainty:

    * a symbol or requirement id prefix — ``CanSM_SetBaudrate``,
      ``SWS_CANIF_00023`` — which is unambiguous;
    * the document's title, because people write "CAN Interface", not "CanIf";
    * the bare module name as a whole word — but **only** when it is not also
      the start of another module's name. "Can" prefixes CanIf, CanTp and
      CanSM, so "the CAN controller" names no module in particular, and
      reading it as the driver would exclude the other three documents on the
      strength of a word almost every question here contains.

    Being wrong in the permissive direction costs a wider search. Being wrong
    in the restrictive direction costs the answer.
    """
    lowered = question.casefold()
    token = module.casefold()

    if f"{token}_" in lowered:
        return True

    for document in manifest.documents:
        if document.module != module:
            continue
        title = document.title.casefold()
        # Manifest titles read "Specification of CAN Interface"; a person
        # writes the half that names the thing.
        for phrase in {title, title.replace("specification of ", "").strip()}:
            if phrase and phrase in lowered:
                return True

    prefixes_another = any(
        other.casefold().startswith(token) and other != module
        for other in known_modules(manifest)
    )
    if not prefixes_another:
        return re.search(rf"\b{re.escape(token)}\b", lowered) is not None

    # A module whose name starts another's — "Can" against CanIf/CanTp/CanSM —
    # is matched **case-sensitively**, in its own spelling. This corpus writes
    # the module `Can`, the bus `CAN`, and the English verb `can`, and that is
    # the only thing separating "which scheduled functions does Can have?"
    # (names the module) from "how does the CAN bus signal an error?" (names
    # the bus) and "where can I find..." (names nothing). Getting it wrong
    # here only widens the search, which is the safe direction.
    return re.search(rf"\b{re.escape(module)}\b", question) is not None


def _resolve_module(raw: str, manifest: ProjectManifest) -> tuple[str | None, str | None]:
    """Match ``raw`` to a known module, or say why it could not be matched.

    Accepts the module's own spelling in any case (``canif`` → ``CanIf``) and
    the document title a person is more likely to say (``CAN Interface`` →
    ``CanIf``), because the difference between those is not something a user
    should have to know.
    """
    wanted = raw.strip().casefold()
    if not wanted:
        return None, "empty module name"

    for document in manifest.documents:
        if wanted == document.module.casefold():
            return document.module, None
    for document in manifest.documents:
        title = document.title.casefold()
        if wanted == title or wanted in title:
            return document.module, None

    return None, (
        f"{raw!r} is not a module in this corpus (known: {', '.join(known_modules(manifest))})"
    )


_SECTION_PATHS_SQL = """
SELECT DISTINCT section_path FROM requirements
WHERE project_id = ? AND section_path IS NOT NULL
ORDER BY section_path
"""


def _resolve_sections(
    hint: str,
    conn: sqlite3.Connection,
    project_id: str,
    *,
    source_docs: frozenset[str] | None,
) -> tuple[tuple[str, ...], str | None]:
    """Section paths containing ``hint``, narrowed to ``source_docs`` if given.

    Narrowing by document matters: "scheduled functions in the driver" must not
    also select the CAN Interface's own "Scheduled functions" section, which a
    substring match alone would.
    """
    cleaned = hint.strip()
    if not cleaned:
        return (), "empty section hint"
    if len(cleaned) > MAX_SECTION_LENGTH:
        return (), f"section hint is longer than {MAX_SECTION_LENGTH} characters"

    if source_docs is None:
        rows = conn.execute(_SECTION_PATHS_SQL, (project_id,))
    else:
        placeholders = ", ".join("?" * len(source_docs))
        rows = conn.execute(
            "SELECT DISTINCT section_path FROM requirements "
            f"WHERE project_id = ? AND section_path IS NOT NULL "
            f"AND source_doc IN ({placeholders}) ORDER BY section_path",
            (project_id, *sorted(source_docs)),
        )

    needle = cleaned.casefold()
    matched = tuple(
        row["section_path"] for row in rows if needle in row["section_path"].casefold()
    )
    if not matched:
        return (), f"no section in this corpus contains {cleaned!r}"
    if len(matched) > MAX_RESOLVED_SECTIONS:
        return (), (
            f"{cleaned!r} matches {len(matched)} sections, too many to narrow anything"
        )
    return matched, None


def resolve(
    raw: QueryFilter,
    *,
    manifest: ProjectManifest,
    conn: sqlite3.Connection,
    project_id: str,
    question: str | None = None,
) -> Filter:
    """Check ``raw`` against the corpus, dropping whatever cannot be satisfied.

    ``question`` is the user's original text, and passing it turns on
    :func:`question_names_module`'s guard. Only the *inferred* path passes it:
    a filter a caller stated is theirs to mean, and second-guessing it would
    answer a different question than the one asked.
    """
    dropped: list[tuple[str, str]] = []

    module: str | None = None
    if raw.module is not None:
        module, reason = _resolve_module(raw.module, manifest)
        if reason is not None:
            dropped.append(("module", reason))
        elif question is not None and not question_names_module(question, module, manifest):
            dropped.append((
                "module",
                f"the question does not name {module}, so the inferred module filter "
                "was a guess and would have excluded the other documents",
            ))
            module = None

    doc_type = raw.doc_type

    section_paths: tuple[str, ...] = ()
    if raw.section is not None:
        # A resolved module restricts which documents' sections can match. Code
        # chunks have no section_path at all, so a section hint alongside
        # ``doc_type=code`` is simply inapplicable.
        source_docs: frozenset[str] | None = None
        if module is not None:
            source_docs = frozenset(
                document.key for document in manifest.documents if document.module == module
            )
        if doc_type == CODE_DOC_TYPE:
            dropped.append(("section", "code chunks have no document section"))
        else:
            section_paths, reason = _resolve_sections(
                raw.section, conn, project_id, source_docs=source_docs
            )
            if reason is not None:
                dropped.append(("section", reason))

    return Filter(
        module=module,
        doc_type=doc_type,
        section_paths=section_paths,
        dropped=tuple(dropped),
    )


# --------------------------------------------------------------------------
# extracting it with the model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Extraction:
    """The resolved filter, what the model actually said, and what it cost."""

    filter: Filter
    raw: QueryFilter
    usage: LlmUsage
    fallback_reason: str | None = None


SYSTEM_PROMPT = """\
You extract search filters from one engineering question. You do not answer it.

{corpus}

Modules in this corpus: {modules}.

Fill a field only when the question clearly implies it, and leave it null \
otherwise. A wrong filter is far worse than no filter: it hides the answer \
entirely, while no filter merely returns a slightly broader result.

- module: only when the question names a module or its document.
- doc_type: default to null. Use 'requirement' only when the question \
explicitly asks what is *required*, mandated or specified. Use 'context' only \
when the question asks purely for a definition or terminology — never for a \
"how does X work" question, whose answer is normally a requirement. Use \
'code' only when the question is about the implementation source.
- section: only words that would plausibly appear in a document section \
heading. Never invent one.

Return only the JSON object the schema describes.\
"""


def extract(
    llm: Llm,
    query: str,
    *,
    manifest: ProjectManifest,
    conn: sqlite3.Connection,
    project_id: str,
) -> Extraction:
    """Extract and resolve a filter for ``query``.

    Degrades to an empty filter with ``fallback_reason`` set if the model
    fails or returns something unparseable — an unfiltered search still
    answers the question.
    """
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("cannot extract a filter from an empty question")

    messages = [
        system(
            SYSTEM_PROMPT.format(
                corpus=corpus_description(manifest),
                modules=", ".join(known_modules(manifest)),
            )
        ),
        user(fence(QUESTION_FENCE, cleaned, what="user question")),
    ]

    try:
        result = llm.structured(QueryFilter, messages)
    except LlmError as exc:
        return Extraction(
            filter=Filter(),
            raw=QueryFilter(),
            usage=exc.usage if exc.usage is not None else LlmUsage(purpose=llm.purpose),
            fallback_reason=str(exc),
        )

    return Extraction(
        filter=resolve(
            result.value,
            manifest=manifest,
            conn=conn,
            project_id=project_id,
            question=cleaned,
        ),
        raw=result.value,
        usage=result.usage,
    )
