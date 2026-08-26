"""The in-memory BM25 index, built from SQLite at API startup (story S1.5.2).

Spec §3 says exactly this: ``rank_bm25`` built in memory at API startup.
Nothing is persisted — SQLite is the source of truth and this index is a
derived, disposable structure, so it is never written to disk and never built
during ingestion. Rebuilding it is a few hundred milliseconds; keeping a
second copy of the corpus in sync with SQLite is a bug factory.

BM25 is in the design (spec §4) to catch the exact tokens dense retrieval
blurs: ``Can_Write``, ``CanIf_RxIndication``, ``CAN_E_PARAM_POINTER``,
``ST[11]``. A naive lowercase-alphanumeric split destroys precisely those, so
:func:`tokenize` keeps the whole identifier *and* emits its underscore parts —
see its docstring for what is deliberately **not** split.

:func:`search` is a pure function of ``(index, query)``: story S2.6.1 requires
each pipeline stage to be independently testable, so there is no module-level
index and no hidden state. The startup wiring holds the index and passes it in.

.. note::
   ``BM25Okapi`` returns **all-zero scores** on a tiny corpus and looks
   broken when it is not: Okapi's IDF is ``log((N - n + 0.5) / (n + 0.5))``,
   which is exactly 0 at ``N=2, n=1``. Measured on the same query with the
   tokenizer below — N=2: idf 0.0, every score 0.0; N=4: idf 0.847, top score
   2.905; N=8: idf 1.609, top score 4.41. So the tests use at least eight
   documents and assert on *ranking order*, never on absolute scores. A zero
   score on a two-document fixture is not a tokenizer bug and must not be
   "fixed" by changing the tokenizer.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from rank_bm25 import BM25Okapi

from retrieval.chunks import CODE_DOC_TYPE, code_chunk_key, requirement_chunk_key

#: Runs of letters, digits and underscores. Underscores are *kept* so
#: ``Can_Write`` survives as one token; everything else (dots, brackets,
#: slashes, punctuation) is a separator, which is what turns ``ST[11]`` into
#: ``st`` and ``11`` — both of which a query for ``ST[11]`` also produces.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lower-case tokens, plus the underscore parts of compound identifiers.

    ``Can_Write`` indexes as ``can_write``, ``can`` and ``write``;
    ``CAN_E_PARAM_POINTER`` as the whole token plus ``can``, ``e``, ``param``
    and ``pointer``. Emitting both levels means a query for the exact symbol
    still ranks the exact match first (it matches on every part *and* on the
    whole), while a query for ``write`` can still find it.

    camelCase is deliberately **not** split. ``CanIf_RxIndication`` keeps
    ``canif`` and ``rxindication``, because splitting camel case would shatter
    the module prefixes this corpus is organised around (``CanIf`` → ``can`` +
    ``if``, where ``if`` is pure noise) for no retrieval gain.
    """
    tokens: list[str] = []
    for match in TOKEN_PATTERN.finditer(text):
        token = match.group(0).lower()
        tokens.append(token)
        if "_" in token:
            tokens.extend(part for part in token.split("_") if part)
    return tokens


@dataclass(frozen=True)
class Bm25Record:
    """One indexed chunk, with just enough metadata to render a hit.

    ``id`` is the shared chunk id from :mod:`retrieval.chunks`, so a BM25 hit
    and a dense hit for the same chunk carry the same key and Reciprocal Rank
    Fusion (spec §4) can fuse them.
    """

    id: str
    text: str
    doc_type: str
    #: ``source_doc`` for a requirement, ``repo_path`` for a code unit.
    source: str
    title: str | None = None
    symbol: str | None = None
    page: int | None = None
    line_span: tuple[int, int] | None = None


@dataclass(frozen=True)
class Bm25Index:
    """An immutable BM25 index over a fixed set of records.

    ``okapi`` is ``None`` for an empty corpus — ``BM25Okapi`` divides by the
    document count in its constructor, so it cannot be built at all from zero
    documents, and a fresh clone with no ingested data must still boot.
    """

    records: tuple[Bm25Record, ...]
    okapi: BM25Okapi | None

    def __len__(self) -> int:
        return len(self.records)


@dataclass(frozen=True)
class Bm25Hit:
    """One scored record, at its 1-based rank in the result list."""

    record: Bm25Record
    score: float
    rank: int


def build_index(records: Sequence[Bm25Record]) -> Bm25Index:
    """Tokenize ``records`` and build the Okapi index over them."""
    ordered = tuple(records)
    if not ordered:
        return Bm25Index(records=(), okapi=None)
    corpus = [tokenize(_indexed_text(record)) for record in ordered]
    return Bm25Index(records=ordered, okapi=BM25Okapi(corpus))


def _indexed_text(record: Bm25Record) -> str:
    """What actually gets tokenized for a record.

    The title and the symbol are prepended so an exact-symbol query ranks the
    unit that *is* that symbol above the units that merely call it.
    """
    parts = [part for part in (record.title, record.symbol, record.text) if part]
    return "\n".join(parts)


def search(
    index: Bm25Index,
    query: str,
    *,
    limit: int = 10,
    predicate: Callable[[Bm25Record], bool] | None = None,
) -> list[Bm25Hit]:
    """Top ``limit`` records for ``query``, best first. A pure function.

    Mutates nothing — the same index and query always produce the same
    ranking, and the index can be shared across concurrent requests.
    Zero-scoring records are dropped: a BM25 hit that matched no query term
    is not a hit, and passing it into fusion would dilute the dense side.
    """
    if index.okapi is None or limit < 1:
        return []
    tokens = tokenize(query)
    if not tokens:
        return []

    scores = index.okapi.get_scores(tokens)
    candidates = [
        (float(score), position)
        for position, score in enumerate(scores)
        if score > 0.0 and (predicate is None or predicate(index.records[position]))
    ]
    # Sort by descending score, then by position, so equal scores rank in a
    # stable, reproducible order rather than whatever the sort happens to do.
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [
        Bm25Hit(record=index.records[position], score=score, rank=rank)
        for rank, (score, position) in enumerate(candidates[:limit], start=1)
    ]


# --------------------------------------------------------------------------
# loading from SQLite
# --------------------------------------------------------------------------
#
# Deliberately narrow SELECTs: the index needs the text and a handful of
# display fields, not the JSON blobs. If startup were ever slow the fix is a
# leaner query, not a cache — a cache would be a second copy of the corpus to
# keep in sync with SQLite, which is the thing this design avoids.

_REQUIREMENTS_SQL = """
SELECT id, title, text, doc_type, source_doc, page
FROM requirements
WHERE project_id = ?
ORDER BY id
"""

_CODE_UNITS_SQL = """
SELECT repo_path, kind, symbol, text, line_span_start, line_span_end
FROM code_units
WHERE project_id = ?
ORDER BY repo_path, kind, symbol, line_span_start, line_span_end
"""


def load_records(conn: sqlite3.Connection, project_id: str) -> list[Bm25Record]:
    """Every indexable chunk for ``project_id``, requirements then code.

    The order is deterministic (by id, then by code-unit storage key), so two
    startups over the same database produce identical indexes and therefore
    identical rankings.
    """
    records: list[Bm25Record] = []
    for row in conn.execute(_REQUIREMENTS_SQL, (project_id,)):
        records.append(
            Bm25Record(
                id=requirement_chunk_key(row["id"]),
                text=row["text"],
                doc_type=row["doc_type"],
                source=row["source_doc"],
                title=row["title"],
                page=row["page"],
            )
        )
    for row in conn.execute(_CODE_UNITS_SQL, (project_id,)):
        span = (row["line_span_start"], row["line_span_end"])
        records.append(
            Bm25Record(
                id=code_chunk_key(row["repo_path"], row["kind"], row["symbol"], span),
                text=row["text"],
                doc_type=CODE_DOC_TYPE,
                source=row["repo_path"],
                symbol=row["symbol"],
                line_span=span,
            )
        )
    return records


def build_from_sqlite(conn: sqlite3.Connection, project_id: str) -> Bm25Index:
    """Load ``project_id``'s chunks from ``conn`` and build the index."""
    return build_index(load_records(conn, project_id))
