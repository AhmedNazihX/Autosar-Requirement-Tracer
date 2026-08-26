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
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from core.models import CodeUnit, ReqAnnotation, Requirement

SCHEMA_VERSION = 1

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
    PRIMARY KEY (project_id, repo_path, symbol)
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
    events TEXT NOT NULL DEFAULT '[]'
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
ON CONFLICT (project_id, repo_path, symbol) DO UPDATE SET
    language = excluded.language,
    kind = excluded.kind,
    line_span_start = excluded.line_span_start,
    line_span_end = excluded.line_span_end,
    text = excluded.text,
    req_annotations = excluded.req_annotations,
    git_sha = excluded.git_sha
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    """Create all tables/indexes if missing. Idempotent — safe to call twice."""
    conn.executescript(_DDL)
    row = conn.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
    if row["n"] == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


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
    conn: sqlite3.Connection, project_id: str, repo_path: str, symbol: str
) -> CodeUnit | None:
    row = conn.execute(
        "SELECT * FROM code_units WHERE project_id = ? AND repo_path = ? AND symbol = ?",
        (project_id, repo_path, symbol),
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
) -> str:
    """Create a message. ``events`` is the message's full SSE event stream (spec §11).

    ``parent_message_id`` is nullable and unused by v1 features — it exists
    so a future rollback/branching feature does not require a migration.
    """
    message_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO messages (id, thread_id, role, parent_message_id, created_at, events) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, thread_id, role, parent_message_id, _now_iso(), json.dumps(events or [])),
    )
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
# --------------------------------------------------------------------------


def get_embedding(
    conn: sqlite3.Connection, content_hash: str, model_id: str
) -> list[float] | None:
    row = conn.execute(
        "SELECT embedding FROM embedding_cache WHERE content_hash = ? AND model_id = ?",
        (content_hash, model_id),
    ).fetchone()
    return json.loads(row["embedding"]) if row is not None else None


def put_embedding(
    conn: sqlite3.Connection, content_hash: str, model_id: str, embedding: list[float]
) -> None:
    conn.execute(
        """
        INSERT INTO embedding_cache (content_hash, model_id, embedding, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (content_hash, model_id) DO UPDATE SET
            embedding = excluded.embedding,
            created_at = excluded.created_at
        """,
        (content_hash, model_id, json.dumps(embedding), _now_iso()),
    )
    conn.commit()
