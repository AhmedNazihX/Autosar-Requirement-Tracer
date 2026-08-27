"""``api.deps.load_state`` — the boot path the API actually runs.

Ported from the tests of ``retrieval.startup`` when that module was deleted:
it was WP1's boot wiring, superseded by ``load_state`` once the lifespan
needed Chroma and page counts too, and its tests were the only coverage of
the degradation contract — the API boots with no database, a corrupt one, or
a locked one (story S3.6.1 can only report a broken index from a server that
is up). That contract belongs to the code that serves it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from api import deps
from core import db
from retrieval import bm25
from tests.support_engine import MANIFEST, build_index, close_index


def _load(
    monkeypatch: pytest.MonkeyPatch, db_path: Path, chroma_dir: Path
) -> deps.AppState:
    """Run ``load_state`` against a tmp corpus.

    ``load_state`` resolves everything through the manifest, so the seams are
    the manifest loader and the two path helpers — patched rather than passed,
    because production callers hold only a manifest path.
    """
    monkeypatch.setattr(deps, "load_manifest", lambda _: MANIFEST)
    monkeypatch.setattr(deps.paths, "db_path", lambda _: db_path)
    monkeypatch.setattr(deps.paths, "chroma_dir", lambda _: chroma_dir)
    return deps.load_state("ignored-by-the-patches.yaml")


def test_boot_loads_the_corpus_and_serves(tmp_path: Path, monkeypatch):
    parts = build_index(tmp_path)
    close_index(parts)  # load_state opens its own pool over the same files

    state = _load(monkeypatch, tmp_path / "reqtrace.db", tmp_path / "chroma")
    try:
        assert state.ready
        assert state.records > 0
        assert state.error is None
        assert state.page_counts, "the pane needs page counts at citation time"
        assert bm25.search(state.bm25_index, "Can_Write")
    finally:
        state.close()


def test_boot_without_a_database_reports_how_to_build_one(tmp_path: Path, monkeypatch):
    """A fresh clone has no ``data/``; the API must still boot."""
    state = _load(monkeypatch, tmp_path / "absent.db", tmp_path / "chroma")

    assert not state.ready
    assert state.records == 0
    assert "ingestion.run" in (state.error or "")
    assert bm25.search(state.bm25_index, "Can_Write") == []


def test_boot_with_a_corrupt_database_reports_it_instead_of_dying(
    tmp_path: Path, monkeypatch
):
    """A truncated or overwritten file must not kill the lifespan."""
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a database, it is 40 bytes of noise")

    state = _load(monkeypatch, path, tmp_path / "chroma")

    assert not state.ready
    assert state.records == 0
    assert "could not open the index" in (state.error or "")
    assert "DatabaseError" in (state.error or "")


def test_boot_with_a_locked_database_reports_it_instead_of_dying(
    tmp_path: Path, monkeypatch
):
    """The WAL-lock case (`make dev` during ingestion), forced deterministically."""
    path = tmp_path / "locked.db"
    connection = db.connect(path)
    db.migrate(connection)

    def locked(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(deps.db, "migrate", locked)
    state = _load(monkeypatch, path, tmp_path / "chroma")
    connection.close()

    assert not state.ready
    assert "could not open the index" in (state.error or "")
    assert "database is locked" in (state.error or "")
