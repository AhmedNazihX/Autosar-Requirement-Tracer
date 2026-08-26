"""Boot-time index wiring (story S1.5.2's ``startup < 5s`` criterion).

The BM25 index is derived, in-memory and disposable (spec §3), so *something*
has to build it when the API comes up. That something is here rather than in
``api/main.py`` so it can be timed and tested without an ASGI client, and so
the index stays a value the caller holds rather than module state — spec §4
requires each retrieval stage to be a pure function over an index passed in.

A missing database is not an error: a fresh clone has no ``data/`` at all, and
the API must still boot so the first-run setup screen (story S3.6.1) can tell
the user to run ingestion. :func:`load_indexes` reports that as
``records == 0`` with ``bm25`` still usable (and empty), never a crash.

Neither is an *unreadable* one. ``make dev`` while ingestion is running is
enough to meet a WAL-locked database, and a truncated file is enough to meet a
corrupt one; both used to escape the lifespan and kill the server, which is
precisely when the first-run screen needs the server up in order to say what is
wrong. Every load path here degrades to a reported ``error`` instead.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from core import db, paths
from core.manifest import ManifestError, ProjectManifest, load_manifest
from retrieval import bm25


@dataclass(frozen=True)
class StartupIndexes:
    """What boot produced, and how long it took.

    ``elapsed_seconds`` is the number story S1.5.2 is measured against, so it
    is a first-class field rather than a log line.
    """

    project_id: str
    bm25: bm25.Bm25Index
    records: int
    elapsed_seconds: float
    db_path: Path | None = None
    #: Set when the indexes could not be built. The API still boots.
    error: str | None = None

    @property
    def ready(self) -> bool:
        """True when there is a corpus to search."""
        return self.error is None and self.records > 0


def load_indexes(manifest: ProjectManifest, db_path: Path | None = None) -> StartupIndexes:
    """Build the BM25 index for ``manifest`` from SQLite, timing the work."""
    started = time.perf_counter()
    path = db_path if db_path is not None else paths.db_path(manifest)
    if not path.is_file():
        return StartupIndexes(
            project_id=manifest.project_id,
            bm25=bm25.build_index([]),
            records=0,
            elapsed_seconds=time.perf_counter() - started,
            db_path=path,
            error=(
                f"no index at {path} — run "
                "'uv run python -m ingestion.run ../projects/autosar-can/project.yaml' "
                "to build it"
            ),
        )

    try:
        conn = db.connect(path)
        try:
            db.migrate(conn)
            index = bm25.build_from_sqlite(conn, manifest)
        finally:
            conn.close()
    except (sqlite3.Error, OSError) as exc:
        # A locked (ingestion is running), corrupt or unreadable database. The
        # server must still come up: story S3.6.1's first-run screen reports
        # this error, and it cannot report anything if the process died.
        return StartupIndexes(
            project_id=manifest.project_id,
            bm25=bm25.build_index([]),
            records=0,
            elapsed_seconds=time.perf_counter() - started,
            db_path=path,
            error=(
                f"could not read the index at {path} ({type(exc).__name__}: {exc}) — "
                "if ingestion is running, wait for it to finish and restart; otherwise "
                "delete the file and re-run "
                "'uv run python -m ingestion.run ../projects/autosar-can/project.yaml'"
            ),
        )
    return StartupIndexes(
        project_id=manifest.project_id,
        bm25=index,
        records=len(index),
        elapsed_seconds=time.perf_counter() - started,
        db_path=path,
    )


def load_indexes_from_settings(manifest_path: str | Path) -> StartupIndexes:
    """:func:`load_indexes` for a manifest path, tolerating a bad manifest.

    Used by the API lifespan, where an unreadable manifest must degrade to a
    reported error rather than prevent the process from serving ``/health``.
    """
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        return StartupIndexes(
            project_id="",
            bm25=bm25.build_index([]),
            records=0,
            elapsed_seconds=0.0,
            error=str(exc),
        )
    return load_indexes(manifest)
