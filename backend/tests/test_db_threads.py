"""Tests for using SQLite from more than one thread (finding C10, extended).

Finding C10 recorded that a connection cannot cross threads and concluded "build
the Engine per request". That was not enough, and this file exists because the
agent proved it: **LangGraph runs tool nodes on a thread pool**, so a tool
reading SQLite is already on a different thread than the request that opened the
connection — inside a single request. Every tool call failed with
``ProgrammingError: SQLite objects created in a thread can only be used in that
same thread``.

Two fixes were considered and one of them is a trap:

* ``check_same_thread=False`` alone. ``sqlite3.threadsafety`` is 3 on this
  build ("serialized"), which reads like permission to share a connection.
  Measured: 8 threads doing 40 reads each on one shared connection produced
  **6 errors in 101 reads** — ``InterfaceError: bad parameter or other API
  misuse`` — and one read returned the wrong thing. ``Connection.execute``
  allocates an implicit cursor, and concurrent implicit-cursor use races. The
  wrong-result case is the dangerous one: it would have been an occasional
  mystery in production, not a crash.
* **One connection per thread**, which is what :func:`core.db.pooled` does.
  Each thread gets its own connection to the same file; WAL already allows
  concurrent readers, and SQLite serialises the writes.

So the assertions below are about *correctness under concurrency*, not just
about not raising.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from core import db
from core.models import Requirement

PROJECT = "autosar-can"


def _requirement(req_id: str) -> Requirement:
    return Requirement(
        id=req_id,
        text=f"Body of {req_id}.",
        page=1,
        source_doc="can_driver",
        version="R23-11",
        project_id=PROJECT,
    )


@pytest.fixture
def populated(tmp_path: Path):
    path = tmp_path / "threads.db"
    conn = db.connect(path)
    db.migrate(conn)
    db.bulk_insert_requirements(conn, [_requirement(f"SWS_Can_{n:05d}") for n in range(1, 21)])
    conn.close()
    return path


def run_threads(target, count: int = 8) -> list[str]:
    """Run ``target`` on ``count`` threads; return the errors it raised."""
    errors: list[str] = []

    def wrapped() -> None:
        try:
            target()
        except Exception as exc:  # noqa: BLE001 - the point is to collect them
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=wrapped) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


# --------------------------------------------------------------------------
# the problem, pinned down so the fix cannot be undone quietly
# --------------------------------------------------------------------------


def test_a_plain_connection_cannot_be_used_from_another_thread(populated):
    """The failure the agent hit on every tool call."""
    conn = db.connect(populated)
    try:
        errors = run_threads(
            lambda: db.get_requirement(conn, PROJECT, "SWS_Can_00001"), count=1
        )
        assert errors and "same thread" in errors[0]
    finally:
        conn.close()


def test_sharing_one_connection_across_threads_is_not_made_safe_by_threadsafety_3(
    populated,
):
    """``check_same_thread=False`` is not a fix, and this proves it empirically.

    Marked as the reason :func:`core.db.pooled` exists. If a future change
    "simplifies" the pool away in favour of this flag, this test fails.
    """
    shared = sqlite3.connect(str(populated), check_same_thread=False)
    shared.row_factory = sqlite3.Row
    seen: list[object] = []

    def hammer() -> None:
        for _ in range(60):
            row = shared.execute(
                "SELECT id FROM requirements WHERE canonical_id = ?", ("SWS_CAN_00001",)
            ).fetchone()
            seen.append(None if row is None else row["id"])

    errors = run_threads(hammer)
    shared.close()

    misbehaved = bool(errors) or any(value != "SWS_Can_00001" for value in seen)
    assert misbehaved, (
        "if this ever stops failing, sqlite3's implicit-cursor race has been "
        "fixed upstream and core.db.pooled could be reconsidered"
    )


# --------------------------------------------------------------------------
# the fix
# --------------------------------------------------------------------------


def test_a_pooled_connection_works_from_many_threads(populated):
    pool = db.pooled(populated)
    seen: list[str] = []
    lock = threading.Lock()

    def read() -> None:
        for _ in range(60):
            found = db.get_requirement(pool, PROJECT, "SWS_Can_00001")
            with lock:
                seen.append(found.id)

    errors = run_threads(read)
    pool.close_all()

    assert errors == []
    assert len(seen) == 8 * 60
    assert set(seen) == {"SWS_Can_00001"}, "every read returned the right row"


def test_each_thread_gets_its_own_connection(populated):
    pool = db.pooled(populated)
    identities: set[int] = set()
    lock = threading.Lock()

    def note() -> None:
        with lock:
            identities.add(id(pool.connection()))

    run_threads(note, count=4)
    pool.close_all()

    assert len(identities) == 4, "one connection per thread, not one shared"


def test_the_same_thread_reuses_its_connection(populated):
    pool = db.pooled(populated)
    try:
        assert pool.connection() is pool.connection()
    finally:
        pool.close_all()


def test_a_pooled_connection_can_write_from_a_thread(populated):
    """Tools write too — the embedding cache is populated during retrieval."""
    pool = db.pooled(populated)

    def write() -> None:
        for n in range(5):
            db.put_embedding(pool, f"hash-{threading.get_ident()}-{n}", "m", [0.5, 0.5])

    errors = run_threads(write, count=4)
    total = pool.execute("SELECT COUNT(*) AS n FROM embedding_cache").fetchone()["n"]
    pool.close_all()

    assert errors == []
    assert total == 20


def test_pragmas_are_applied_to_every_threads_connection(populated):
    """A connection without ``foreign_keys`` would silently skip cascades."""
    pool = db.pooled(populated)
    modes: list[tuple[int, str]] = []
    lock = threading.Lock()

    def check() -> None:
        conn = pool.connection()
        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        with lock:
            modes.append((fk, journal))

    run_threads(check, count=3)
    pool.close_all()

    assert all(entry == (1, "wal") for entry in modes), modes


def test_the_pool_reports_the_path_it_serves(populated):
    pool = db.pooled(populated)
    try:
        assert Path(pool.path) == populated
    finally:
        pool.close_all()


def test_migrate_accepts_a_pool(populated):
    """The API migrates at startup through whatever handle it holds."""
    pool = db.pooled(populated)
    try:
        db.migrate(pool)
        version = pool.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()["v"]
        assert version == db.SCHEMA_VERSION
    finally:
        pool.close_all()
