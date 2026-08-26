"""SQLite storage: schema migration + CRUD.

Per the design spec (§7 storage, §11 threads), SQLite holds the requirement/
code-unit registry, threads with their full per-message SSE event streams,
report runs, the verdict cache keyed ``(req_id, git_sha, model_id)`` (§5),
and the content-hash-keyed embedding cache. Vectors live in Chroma and the
BM25 index is built in-memory at startup — neither is this module's concern.

This is a plain module of functions taking an explicit
``sqlite3.Connection`` — no ORM, no global singleton, per controller ruling
R10. Callers only ever see :class:`~core.models.Requirement` and
:class:`~core.models.CodeUnit` in and out of the requirement/code-unit CRUD;
list-valued fields are (de)serialized to JSON text here.

**Cached vectors are float32 BLOBs, never JSON.** See
:func:`get_embedding` / :func:`put_embeddings` — the encoding is the one
part of this module where getting it wrong is silent rather than loud, so it
is spelled out there.
"""

from __future__ import annotations

import array
import json
import sqlite3
import sys
import threading
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from core.models import CodeUnit, ReqAnnotation, Requirement

#: 1 — the original schema.
#: 2 — ``embedding_cache.embedding`` holds a float32 BLOB instead of a JSON
#:     text array. No DDL changed (TEXT affinity stores a BLOB as-is); the
#:     migration exists only to discard rows written under version 1, which
#:     are then recomputed. See :func:`_discard_legacy_embeddings`.
SCHEMA_VERSION = 3

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements (
    project_id TEXT NOT NULL,
    id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    title TEXT,
    text TEXT NOT NULL,
    section_path TEXT,
    page INTEGER NOT NULL,
    bbox TEXT,
    char_span TEXT,
    source_doc TEXT NOT NULL,
    upstream_ids TEXT NOT NULL DEFAULT '[]',
    named_symbols TEXT NOT NULL DEFAULT '[]',
    version TEXT NOT NULL,
    doc_type TEXT NOT NULL DEFAULT 'requirement',
    PRIMARY KEY (project_id, id)
);
CREATE INDEX IF NOT EXISTS idx_requirements_canonical_id
    ON requirements (project_id, canonical_id);
CREATE INDEX IF NOT EXISTS idx_requirements_source_doc
    ON requirements (project_id, source_doc);
CREATE INDEX IF NOT EXISTS idx_requirements_doc_type
    ON requirements (project_id, doc_type);

CREATE TABLE IF NOT EXISTS code_units (
    project_id TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    symbol TEXT NOT NULL,
    language TEXT NOT NULL,
    kind TEXT NOT NULL,
    line_span_start INTEGER NOT NULL,
    line_span_end INTEGER NOT NULL,
    text TEXT NOT NULL,
    req_annotations TEXT NOT NULL DEFAULT '[]',
    git_sha TEXT NOT NULL,
    PRIMARY KEY (project_id, repo_path, kind, symbol, line_span_start, line_span_end)
);
CREATE INDEX IF NOT EXISTS idx_code_units_symbol
    ON code_units (project_id, symbol);
CREATE INDEX IF NOT EXISTS idx_code_units_repo_path
    ON code_units (project_id, repo_path);

CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads (id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    parent_message_id TEXT REFERENCES messages (id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    events TEXT NOT NULL DEFAULT '[]',
    -- The message's text. Derivable from `events` for an assistant message by
    -- joining its `token` deltas, but NOT for a user message, which has no
    -- events at all — so it is stored rather than computed. Matches
    -- `StoredMessage.content` in frontend/lib/threads.ts.
    content TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_thread_id ON messages (thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent_message_id
    ON messages (parent_message_id);

CREATE TABLE IF NOT EXISTS report_runs (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    est_cost_usd REAL,
    actual_cost_usd REAL,
    result TEXT
);
CREATE INDEX IF NOT EXISTS idx_report_runs_project_id
    ON report_runs (project_id);

CREATE TABLE IF NOT EXISTS verdict_cache (
    req_id TEXT NOT NULL,
    git_sha TEXT NOT NULL,
    model_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (req_id, git_sha, model_id)
);

-- Per-document facts that are neither in the manifest nor derivable at
-- request time. `page_count` is the only one so far and it exists because
-- `RequirementCitation.page_count` (frontend/lib/events.ts) is a required
-- number: it cannot be read from a PDF that ingestion has since deleted, and
-- the manifest should not carry a figure derived from the file it names.
-- Ingestion knows it while the document is open, so ingestion records it.
CREATE TABLE IF NOT EXISTS documents (
    project_id TEXT NOT NULL,
    doc_key TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, doc_key)
);

-- ``embedding`` holds a float32 BLOB (schema version 2). The declared type
-- is left as TEXT deliberately: SQLite's TEXT affinity stores a BLOB value
-- as-is, so changing the encoding needed no DDL change and no rewrite of
-- this table's definition. ``typeof(embedding)`` is therefore the authority
-- on what a row actually holds, and the reader keys on it.
CREATE TABLE IF NOT EXISTS embedding_cache (
    content_hash TEXT NOT NULL,
    model_id TEXT NOT NULL,
    embedding TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (content_hash, model_id)
);
"""

_UPSERT_REQUIREMENT_SQL = """
INSERT INTO requirements (
    project_id, id, canonical_id, title, text, section_path, page,
    bbox, char_span, source_doc, upstream_ids, named_symbols, version, doc_type
) VALUES (
    :project_id, :id, :canonical_id, :title, :text, :section_path, :page,
    :bbox, :char_span, :source_doc, :upstream_ids, :named_symbols, :version, :doc_type
)
ON CONFLICT (project_id, id) DO UPDATE SET
    canonical_id = excluded.canonical_id,
    title = excluded.title,
    text = excluded.text,
    section_path = excluded.section_path,
    page = excluded.page,
    bbox = excluded.bbox,
    char_span = excluded.char_span,
    source_doc = excluded.source_doc,
    upstream_ids = excluded.upstream_ids,
    named_symbols = excluded.named_symbols,
    version = excluded.version,
    doc_type = excluded.doc_type
"""

_UPSERT_CODE_UNIT_SQL = """
INSERT INTO code_units (
    project_id, repo_path, symbol, language, kind,
    line_span_start, line_span_end, text, req_annotations, git_sha
) VALUES (
    :project_id, :repo_path, :symbol, :language, :kind,
    :line_span_start, :line_span_end, :text, :req_annotations, :git_sha
)
ON CONFLICT (project_id, repo_path, kind, symbol, line_span_start, line_span_end) DO UPDATE SET
    language = excluded.language,
    text = excluded.text,
    req_annotations = excluded.req_annotations,
    git_sha = excluded.git_sha
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ConnectionPool:
    """One SQLite connection per thread, all onto the same file.

    Needed because the agent is multi-threaded *within* a single request:
    LangGraph runs tool nodes on a thread pool, so a tool reading SQLite is
    already on a different thread from the request that opened the connection.
    A plain connection raises ``ProgrammingError: SQLite objects created in a
    thread can only be used in that same thread``.

    **Why not ``check_same_thread=False``.** ``sqlite3.threadsafety`` is 3
    ("serialized") on this build, which reads like permission to share one
    connection. Measured: 8 threads × 40 reads on a shared connection gave 6
    errors — ``InterfaceError: bad parameter or other API misuse`` — and one
    read returned the wrong row. ``Connection.execute`` allocates an implicit
    cursor and concurrent implicit-cursor use races. The wrong-row case is the
    dangerous one: an occasional mystery rather than a crash.
    ``tests/test_db_threads.py`` pins that down so the pool cannot be
    "simplified" away.

    Quacks like a connection for the handful of methods this codebase uses, so
    every ``db.<function>(conn, ...)`` call site works unchanged.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = str(db_path)
        self._local = threading.local()
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def connection(self) -> sqlite3.Connection:
        """This thread's connection, opening it on first use."""
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = connect(self.path)
            self._local.conn = existing
            # Tracked so close_all() can release them; the list is only ever
            # appended to and drained, and both are under the lock.
            with self._lock:
                self._all.append(existing)
        return existing

    # -- the connection surface this codebase actually uses ----------------

    def execute(self, *args, **kwargs):
        return self.connection().execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self.connection().executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self.connection().executescript(*args, **kwargs)

    def commit(self) -> None:
        self.connection().commit()

    def rollback(self) -> None:
        self.connection().rollback()

    def cursor(self):
        return self.connection().cursor()

    @property
    def row_factory(self):
        return self.connection().row_factory

    def close_all(self) -> None:
        """Close every connection this pool opened, on any thread.

        Closing another thread's connection is allowed — it is the *use* of one
        that is thread-bound, not the close.
        """
        with self._lock:
            connections, self._all = self._all, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:
                # Already closed, or its thread died holding it. Nothing useful
                # to do at shutdown.
                pass
        self._local = threading.local()


def pooled(db_path: str | Path) -> ConnectionPool:
    """A :class:`ConnectionPool` for ``db_path``. Safe to share across threads."""
    return ConnectionPool(db_path)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with the project-wide pragmas set.

    Every connection gets ``foreign_keys = ON`` (so thread/message cascade
    deletes work) and ``journal_mode = WAL``.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if missing, then apply pending migrations.

    Idempotent — safe to call twice, and safe to call on a database written by
    an older version of this module.
    """
    conn.executescript(_DDL)
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(version) AS version FROM schema_version"
    ).fetchone()
    if row["n"] == 0:
        # A database this module just created: nothing to migrate, and no
        # legacy row can exist in it.
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
        return

    from_version = int(row["version"])
    if from_version >= SCHEMA_VERSION:
        conn.commit()
        return
    if from_version < 2:
        _discard_legacy_embeddings(conn)
    if from_version < 3:
        # `CREATE TABLE IF NOT EXISTS` above already added `documents`, but it
        # cannot add a column to a table that already exists.
        _add_column_if_missing(conn, "messages", "content", "TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, definition: str
) -> bool:
    """``ALTER TABLE ... ADD COLUMN`` unless the column is already there.

    SQLite has no ``ADD COLUMN IF NOT EXISTS``, and ``migrate`` must stay
    idempotent, so the column list is checked first. Table and column names are
    interpolated because SQLite does not parameterise identifiers; both are
    module-level literals, never caller input.
    """
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return True


def _discard_legacy_embeddings(conn: sqlite3.Connection) -> int:
    """Delete schema-version-1 (JSON text) rows from ``embedding_cache``.

    They are *discarded* rather than read: a JSON row and a float32 blob are
    both just bytes in a TEXT-affinity column, and decoding one as the other
    would hand retrieval a plausible-looking garbage vector — the worst
    failure available here. Deleting them costs one re-embedding of the
    corpus (measured at $0.0073 for 3356 chunks), which is cheaper than
    carrying a second decoder forever.

    The space the deleted rows free is reused by the blobs written next, so
    the file does not grow; it also does not shrink without an explicit
    ``VACUUM``, which is deliberately not run here — story S1.5.2 requires
    API startup under 5 seconds and this function is on that path.
    """
    deleted = conn.execute(
        "DELETE FROM embedding_cache WHERE typeof(embedding) <> 'blob'"
    ).rowcount
    return deleted if deleted > 0 else 0


# --------------------------------------------------------------------------
# requirements
# --------------------------------------------------------------------------


def _requirement_params(req: Requirement) -> dict:
    return {
        "project_id": req.project_id,
        "id": req.id,
        "canonical_id": Requirement.canonical_id(req.id),
        "title": req.title,
        "text": req.text,
        "section_path": req.section_path,
        "page": req.page,
        "bbox": json.dumps(list(req.bbox)) if req.bbox is not None else None,
        "char_span": json.dumps(list(req.char_span)) if req.char_span is not None else None,
        "source_doc": req.source_doc,
        "upstream_ids": json.dumps(req.upstream_ids),
        "named_symbols": json.dumps(req.named_symbols),
        "version": req.version,
        "doc_type": req.doc_type,
    }


def _row_to_requirement(row: sqlite3.Row) -> Requirement:
    bbox = json.loads(row["bbox"]) if row["bbox"] is not None else None
    char_span = json.loads(row["char_span"]) if row["char_span"] is not None else None
    return Requirement(
        id=row["id"],
        title=row["title"],
        text=row["text"],
        section_path=row["section_path"],
        page=row["page"],
        bbox=tuple(bbox) if bbox is not None else None,
        char_span=tuple(char_span) if char_span is not None else None,
        source_doc=row["source_doc"],
        upstream_ids=json.loads(row["upstream_ids"]),
        named_symbols=json.loads(row["named_symbols"]),
        version=row["version"],
        project_id=row["project_id"],
        doc_type=row["doc_type"],
    )


def upsert_requirement(conn: sqlite3.Connection, req: Requirement) -> None:
    conn.execute(_UPSERT_REQUIREMENT_SQL, _requirement_params(req))
    conn.commit()


def bulk_insert_requirements(conn: sqlite3.Connection, requirements: Iterable[Requirement]) -> None:
    conn.executemany(_UPSERT_REQUIREMENT_SQL, [_requirement_params(r) for r in requirements])
    conn.commit()


def get_requirement(conn: sqlite3.Connection, project_id: str, req_id: str) -> Requirement | None:
    """Exact, case-insensitive lookup by id. Never semantic — SQLite only."""
    row = conn.execute(
        "SELECT * FROM requirements WHERE project_id = ? AND canonical_id = ?",
        (project_id, Requirement.canonical_id(req_id)),
    ).fetchone()
    return _row_to_requirement(row) if row is not None else None


# --------------------------------------------------------------------------
# code_units
# --------------------------------------------------------------------------


def _code_unit_params(unit: CodeUnit) -> dict:
    return {
        "project_id": unit.project_id,
        "repo_path": unit.repo_path,
        "symbol": unit.symbol,
        "language": unit.language,
        "kind": unit.kind,
        "line_span_start": unit.line_span[0],
        "line_span_end": unit.line_span[1],
        "text": unit.text,
        "req_annotations": json.dumps([a.model_dump() for a in unit.req_annotations]),
        "git_sha": unit.git_sha,
    }


def _row_to_code_unit(row: sqlite3.Row) -> CodeUnit:
    annotations = [ReqAnnotation(**a) for a in json.loads(row["req_annotations"])]
    return CodeUnit(
        repo_path=row["repo_path"],
        language=row["language"],
        symbol=row["symbol"],
        kind=row["kind"],
        line_span=(row["line_span_start"], row["line_span_end"]),
        text=row["text"],
        req_annotations=annotations,
        git_sha=row["git_sha"],
        project_id=row["project_id"],
    )


def upsert_code_unit(conn: sqlite3.Connection, unit: CodeUnit) -> None:
    conn.execute(_UPSERT_CODE_UNIT_SQL, _code_unit_params(unit))
    conn.commit()


def get_code_unit(
    conn: sqlite3.Connection,
    project_id: str,
    repo_path: str,
    kind: str,
    symbol: str,
    line_span: tuple[int, int],
) -> CodeUnit | None:
    """Exact lookup by the full identity key.

    ``symbol`` is **not** unique within a file (e.g. a struct and the
    typedef that names it can share a symbol string at different AST
    nodes), so the full key — ``kind`` + ``line_span`` alongside
    ``repo_path``/``symbol`` — is required to identify one row. To find
    all candidate units for a bare symbol name (e.g. tier-2 anchor
    retrieval), use :func:`list_code_units_by_symbol` instead.
    """
    row = conn.execute(
        "SELECT * FROM code_units "
        "WHERE project_id = ? AND repo_path = ? AND kind = ? AND symbol = ? "
        "AND line_span_start = ? AND line_span_end = ?",
        (project_id, repo_path, kind, symbol, line_span[0], line_span[1]),
    ).fetchone()
    return _row_to_code_unit(row) if row is not None else None


def list_code_units_by_symbol(
    conn: sqlite3.Connection, project_id: str, symbol: str
) -> list[CodeUnit]:
    """Tier-2 anchor retrieval: exact symbol lookup across code units."""
    rows = conn.execute(
        "SELECT * FROM code_units WHERE project_id = ? AND symbol = ?",
        (project_id, symbol),
    ).fetchall()
    return [_row_to_code_unit(row) for row in rows]


# --------------------------------------------------------------------------
# threads + messages
# --------------------------------------------------------------------------


def create_thread(conn: sqlite3.Connection, project_id: str, title: str | None = None) -> str:
    thread_id = uuid.uuid4().hex
    now = _now_iso()
    conn.execute(
        "INSERT INTO threads (id, project_id, title, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (thread_id, project_id, title, now, now),
    )
    conn.commit()
    return thread_id


def list_threads(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM threads WHERE project_id = ? ORDER BY updated_at DESC",
        (project_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_thread(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    return dict(row) if row is not None else None


def rename_thread(conn: sqlite3.Connection, thread_id: str, title: str) -> None:
    conn.execute(
        "UPDATE threads SET title = ?, updated_at = ? WHERE id = ?",
        (title, _now_iso(), thread_id),
    )
    conn.commit()


def delete_thread(conn: sqlite3.Connection, thread_id: str) -> None:
    """Deletes the thread; its messages cascade-delete (FK ON DELETE CASCADE)."""
    conn.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
    conn.commit()


def create_message(
    conn: sqlite3.Connection,
    thread_id: str,
    role: str,
    events: list[dict] | None = None,
    parent_message_id: str | None = None,
    content: str = "",
) -> str:
    """Create a message. ``events`` is the message's full SSE event stream (spec §11).

    ``content`` is the message's text. For an assistant message it is
    reconstructable from the ``token`` events, but for a *user* message there
    are no events to reconstruct from, so it is stored either way — matching
    ``StoredMessage.content`` in ``frontend/lib/threads.ts``. The events remain
    the render source; ``content`` is for exports and auto-titles.

    ``parent_message_id`` is nullable and unused by v1 features — it exists
    so a future rollback/branching feature does not require a migration.
    """
    message_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO messages "
        "(id, thread_id, role, parent_message_id, created_at, events, content) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            message_id,
            thread_id,
            role,
            parent_message_id,
            _now_iso(),
            json.dumps(events or []),
            content,
        ),
    )
    conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (_now_iso(), thread_id))
    conn.commit()
    return message_id


def _row_to_message(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["events"] = json.loads(d["events"])
    return d


def list_messages(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM messages WHERE thread_id = ? ORDER BY created_at ASC",
        (thread_id,),
    ).fetchall()
    return [_row_to_message(row) for row in rows]


def get_message(conn: sqlite3.Connection, message_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    return _row_to_message(row) if row is not None else None


# --------------------------------------------------------------------------
# documents — per-document facts ingestion measured
# --------------------------------------------------------------------------


def put_document_meta(
    conn: sqlite3.Connection, project_id: str, doc_key: str, *, page_count: int
) -> None:
    """Record ``doc_key``'s page count. Idempotent, so a re-ingest is safe."""
    conn.execute(
        "INSERT INTO documents (project_id, doc_key, page_count, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT (project_id, doc_key) DO UPDATE SET "
        "page_count = excluded.page_count, updated_at = excluded.updated_at",
        (project_id, doc_key, int(page_count), _now_iso()),
    )
    conn.commit()


def get_document_meta(conn: sqlite3.Connection, project_id: str, doc_key: str) -> int | None:
    """``doc_key``'s page count, or ``None`` if ingestion never recorded one."""
    row = conn.execute(
        "SELECT page_count FROM documents WHERE project_id = ? AND doc_key = ?",
        (project_id, doc_key),
    ).fetchone()
    return int(row["page_count"]) if row is not None else None


def page_counts(conn: sqlite3.Connection, project_id: str) -> dict[str, int]:
    """Every recorded page count for ``project_id``, keyed by document key.

    Read once per request rather than per citation — four rows, and a citation
    should not cost a query.
    """
    return {
        row["doc_key"]: int(row["page_count"])
        for row in conn.execute(
            "SELECT doc_key, page_count FROM documents WHERE project_id = ?", (project_id,)
        )
    }


# --------------------------------------------------------------------------
# verdict_cache — keyed exactly (req_id, git_sha, model_id), per spec §5
# --------------------------------------------------------------------------


def get_verdict(
    conn: sqlite3.Connection, req_id: str, git_sha: str, model_id: str
) -> dict | None:
    row = conn.execute(
        "SELECT verdict FROM verdict_cache WHERE req_id = ? AND git_sha = ? AND model_id = ?",
        (req_id, git_sha, model_id),
    ).fetchone()
    return json.loads(row["verdict"]) if row is not None else None


def put_verdict(
    conn: sqlite3.Connection,
    req_id: str,
    git_sha: str,
    model_id: str,
    verdict: dict,
    project_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO verdict_cache (req_id, git_sha, model_id, project_id, verdict, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (req_id, git_sha, model_id) DO UPDATE SET
            project_id = excluded.project_id,
            verdict = excluded.verdict,
            created_at = excluded.created_at
        """,
        (req_id, git_sha, model_id, project_id, json.dumps(verdict), _now_iso()),
    )
    conn.commit()


# --------------------------------------------------------------------------
# embedding_cache — keyed by content hash + model id
#
# Vectors are stored as **float32 BLOBs**, four bytes per dimension. Under
# schema version 1 they were JSON text, at ~20 characters per float: one
# 1536-dimension vector was a 31,002-character string, and the cache alone
# was 110 MB of a 114 MB database. The same vectors as float32 are 20.6 MB.
#
# **The narrowing to float32 is deliberate, not an oversight.** OpenRouter
# returns JSON numbers that Python parses as float64, so encoding to float32
# loses precision. That is the right trade here: these vectors are only ever
# compared by cosine similarity, where float32 is the industry norm, and
# Chroma — which holds the authoritative copy for search — stores float32
# itself. A round-trip is therefore exact to float32, not to float64.
# (Measured on this corpus, ``text-embedding-3-small`` values are already
# float32-representable, so the narrowing is in practice a no-op.)
#
# Preserving float64 would have cost 41 MB instead of 20.6 MB and bought
# precision nothing downstream can use.
# --------------------------------------------------------------------------

#: float32. Four bytes per dimension, and the reason a blob is 5x smaller
#: than the JSON it replaced.
_VECTOR_TYPECODE = "f"
_VECTOR_ITEM_BYTES = 4


def _encode_vector(vector: Sequence[float]) -> bytes:
    """Pack ``vector`` as little-endian float32 bytes."""
    packed = array.array(_VECTOR_TYPECODE, vector)
    if sys.byteorder != "little":
        # Byte order is normalized so a database file stays readable if it is
        # ever opened on a machine of the opposite endianness.
        packed.byteswap()
    return packed.tobytes()


def _decode_vector(blob: bytes) -> list[float]:
    """Unpack little-endian float32 bytes back into a list of floats."""
    if len(blob) % _VECTOR_ITEM_BYTES != 0:
        raise ValueError(
            f"cached embedding is {len(blob)} byte(s), which is not a whole number of "
            f"float32 values — refusing to decode a truncated vector"
        )
    packed = array.array(_VECTOR_TYPECODE)
    packed.frombytes(blob)
    if sys.byteorder != "little":
        packed.byteswap()
    return packed.tolist()


def get_embedding(
    conn: sqlite3.Connection, content_hash: str, model_id: str
) -> list[float] | None:
    """The cached vector for ``(content_hash, model_id)``, or ``None``.

    ``typeof(embedding) = 'blob'`` is part of the query, not an assertion
    afterwards: a schema-version-1 row holds JSON *text* in the same column,
    and decoding one as float32 bytes would return a garbage vector that
    looks perfectly healthy. Restricting the SELECT means such a row can only
    ever be a cache **miss** — the text is never handed to the decoder at
    all — so the chunk is re-embedded rather than silently mispaired.
    :func:`migrate` deletes those rows outright; this is the second line of
    defence, for a connection that skipped it.
    """
    row = conn.execute(
        "SELECT embedding FROM embedding_cache "
        "WHERE content_hash = ? AND model_id = ? AND typeof(embedding) = 'blob'",
        (content_hash, model_id),
    ).fetchone()
    return _decode_vector(row["embedding"]) if row is not None else None


_UPSERT_EMBEDDING_SQL = """
INSERT INTO embedding_cache (content_hash, model_id, embedding, created_at)
VALUES (?, ?, ?, ?)
ON CONFLICT (content_hash, model_id) DO UPDATE SET
    embedding = excluded.embedding,
    created_at = excluded.created_at
"""


def put_embeddings(
    conn: sqlite3.Connection,
    model_id: str,
    entries: Iterable[tuple[str, Sequence[float]]],
) -> int:
    """Cache ``(content_hash, vector)`` pairs in **one** transaction.

    Returns how many rows were written. One commit for the whole batch rather
    than one per row: a cold ingest of this corpus caches ~3358 vectors, and
    committing each one meant ~3358 fsyncs.

    All-or-nothing on purpose. Every vector is encoded *before* anything is
    executed, and any failure during the write is rolled back, so the cache
    can never end up claiming a vector it does not hold — a false hit would
    pair a chunk with someone else's vector, which is far worse than a slow
    ingest or a lost batch.
    """
    now = _now_iso()
    rows = [
        (content_hash, model_id, _encode_vector(vector), now)
        for content_hash, vector in entries
    ]
    if not rows:
        return 0
    try:
        conn.executemany(_UPSERT_EMBEDDING_SQL, rows)
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return len(rows)


def put_embedding(
    conn: sqlite3.Connection, content_hash: str, model_id: str, embedding: Sequence[float]
) -> None:
    """Cache a single vector. :func:`put_embeddings` for a batch."""
    put_embeddings(conn, model_id, [(content_hash, embedding)])
