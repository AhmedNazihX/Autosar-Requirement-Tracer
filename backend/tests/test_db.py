"""Tests for core/db.py (S1.2.3): idempotent migration + CRUD round-trips."""

from __future__ import annotations

import sqlite3

import pytest

from core import db
from core.models import CodeUnit, ReqAnnotation, Requirement

GIT_SHA_A = "09433770bebb8f27a7b480d7c96d814c68ffed3e"
GIT_SHA_B = "1111111111111111111111111111111111111111"


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "reqtrace.sqlite3")
    db.migrate(connection)
    yield connection
    connection.close()


def _all_table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    return {row["name"] for row in rows}


# --------------------------------------------------------------------------
# migration
# --------------------------------------------------------------------------


def test_migration_creates_expected_tables(conn):
    tables = _all_table_names(conn)
    for expected in (
        "requirements",
        "code_units",
        "threads",
        "messages",
        "report_runs",
        "verdict_cache",
        "embedding_cache",
        "schema_version",
    ):
        assert expected in tables


def test_migration_is_idempotent(tmp_path):
    connection = db.connect(tmp_path / "idempotent.sqlite3")
    db.migrate(connection)
    tables_before = _all_table_names(connection)
    version_rows_before = connection.execute("SELECT * FROM schema_version").fetchall()

    db.migrate(connection)  # run twice

    tables_after = _all_table_names(connection)
    version_rows_after = connection.execute("SELECT * FROM schema_version").fetchall()

    assert tables_before == tables_after
    assert len(version_rows_before) == 1
    assert len(version_rows_after) == 1
    connection.close()


def test_pragmas_enabled(conn):
    fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert fk == 1
    assert journal_mode == "wal"


# --------------------------------------------------------------------------
# requirements CRUD
# --------------------------------------------------------------------------


def _requirement(**overrides) -> Requirement:
    fields = {
        "id": "SWS_Can_00011",
        "title": "Example title",
        "text": "The Can module shall do the thing.",
        "section_path": "7.6 L-PDU reception",
        "page": 42,
        "bbox": (10.0, 20.0, 100.0, 40.0),
        "char_span": (100, 250),
        "source_doc": "can_driver",
        "upstream_ids": ["SRS_Can_01059", "RS_Ids_00810"],
        "named_symbols": ["Can_Write"],
        "version": "R23-11",
        "project_id": "autosar-can",
    }
    fields.update(overrides)
    return Requirement(**fields)


def test_requirement_upsert_and_get_round_trip(conn):
    req = _requirement()
    db.upsert_requirement(conn, req)

    fetched = db.get_requirement(conn, "autosar-can", "SWS_Can_00011")
    assert fetched == req


def test_get_requirement_is_case_insensitive(conn):
    req = _requirement(id="SWS_Can_00011")
    db.upsert_requirement(conn, req)

    assert db.get_requirement(conn, "autosar-can", "sws_can_00011") == req
    assert db.get_requirement(conn, "autosar-can", "SWS_CAN_00011") == req


def test_get_requirement_missing_returns_none(conn):
    assert db.get_requirement(conn, "autosar-can", "SWS_Can_99999") is None


def test_upsert_requirement_overwrites(conn):
    req = _requirement(text="original")
    db.upsert_requirement(conn, req)
    db.upsert_requirement(conn, req.model_copy(update={"text": "updated"}))

    fetched = db.get_requirement(conn, "autosar-can", "SWS_Can_00011")
    assert fetched.text == "updated"


def test_bulk_insert_requirements(conn):
    reqs = [
        _requirement(id="SWS_Can_00011"),
        _requirement(id="SWS_Can_00012", named_symbols=["Can_Read"]),
    ]
    db.bulk_insert_requirements(conn, reqs)

    assert db.get_requirement(conn, "autosar-can", "SWS_Can_00011") == reqs[0]
    assert db.get_requirement(conn, "autosar-can", "SWS_Can_00012") == reqs[1]


# --------------------------------------------------------------------------
# code_units CRUD
# --------------------------------------------------------------------------


def _code_unit(**overrides) -> CodeUnit:
    fields = {
        "repo_path": "communication/CanIf/src/CanIf.c",
        "language": "c",
        "symbol": "CanIf_Transmit",
        "kind": "function",
        "line_span": (100, 150),
        "text": "void CanIf_Transmit(...) { ... }",
        "req_annotations": [
            ReqAnnotation(
                canonical_id="SWS_CANIF_00023",
                raw="4.0.3/CANIF023",
                marker="@",
                claim="claimed_implemented",
                line=110,
            ),
            ReqAnnotation(
                canonical_id="SWS_CANIF_00316",
                raw="4.0.3/CANIF316",
                marker="!",
                claim="claimed_not_implemented",
                line=140,
            ),
        ],
        "git_sha": GIT_SHA_A,
        "project_id": "autosar-can",
    }
    fields.update(overrides)
    return CodeUnit(**fields)


def _get(conn, unit: CodeUnit) -> CodeUnit | None:
    """Fetch a unit back by its full identity key (matches its own fields)."""
    return db.get_code_unit(
        conn, unit.project_id, unit.repo_path, unit.kind, unit.symbol, unit.line_span
    )


def test_code_unit_upsert_and_get_round_trip(conn):
    unit = _code_unit()
    db.upsert_code_unit(conn, unit)

    assert _get(conn, unit) == unit


def test_code_unit_preserves_annotation_polarity(conn):
    unit = _code_unit()
    db.upsert_code_unit(conn, unit)

    assert _get(conn, unit).claimed_implemented_ids == ["SWS_CANIF_00023"]


def test_get_code_unit_missing_returns_none(conn):
    unit = _code_unit()
    assert _get(conn, unit) is None


def test_list_code_units_by_symbol(conn):
    unit_a = _code_unit(repo_path="communication/CanIf/src/CanIf.c", symbol="CanIf_Transmit")
    unit_b = _code_unit(repo_path="communication/CanTp/src/CanTp.c", symbol="CanIf_Transmit")
    db.upsert_code_unit(conn, unit_a)
    db.upsert_code_unit(conn, unit_b)

    results = db.list_code_units_by_symbol(conn, "autosar-can", "CanIf_Transmit")
    assert {u.repo_path for u in results} == {unit_a.repo_path, unit_b.repo_path}


# --------------------------------------------------------------------------
# code_units — same-symbol collisions within one file (fix round 1 regression)
# --------------------------------------------------------------------------
#
# Symbol names are not unique within a single C file: tree-sitter-c emits
# distinct AST nodes that legitimately share a symbol string, e.g. a struct
# definition and the typedef that names it. The primary key must include
# `kind` and `line_span` so these do not silently overwrite each other.


def test_same_symbol_different_kind_and_line_span_both_persist(conn):
    """The core regression test: two units, same file + symbol, different
    kind/line_span. Both must survive the upserts and be independently
    retrievable — previously the second upsert silently clobbered the first.
    """
    struct_unit = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="Can_PduType",
        kind="struct",
        line_span=(10, 12),
        text="struct { uint8 a; } Can_PduType;",
        req_annotations=[],
    )
    typedef_unit = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="Can_PduType",
        kind="typedef",
        line_span=(10, 12),
        text="typedef struct { uint8 a; } Can_PduType;",
        req_annotations=[],
    )

    db.upsert_code_unit(conn, struct_unit)
    db.upsert_code_unit(conn, typedef_unit)

    assert _get(conn, struct_unit) == struct_unit
    assert _get(conn, typedef_unit) == typedef_unit

    results = db.list_code_units_by_symbol(conn, "autosar-can", "Can_PduType")
    assert {u.kind for u in results} == {"struct", "typedef"}


def test_canif_global_three_way_collision_survives(conn):
    """Regression fixture modeled on the real shape the reviewer verified
    with tree-sitter-c against CanIf.c: `struct CanIf_Global {...};`
    followed by `typedef struct CanIf_Global CanIf_GlobalType;` emits
    three AST nodes that can carry the symbol `CanIf_Global` — the struct
    definition, the typedef, and the typedef's inner struct-tag reference.
    All three must persist as distinct rows.
    """
    outer_struct = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="CanIf_Global",
        kind="struct",
        line_span=(1, 5),
        text="struct CanIf_Global { ... };",
        req_annotations=[
            ReqAnnotation(
                canonical_id="SWS_CANIF_00100",
                raw="4.0.3/CANIF100",
                marker="@",
                claim="claimed_implemented",
                line=1,
            )
        ],
    )
    typedef_unit = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="CanIf_Global",
        kind="typedef",
        line_span=(6, 6),
        text="typedef struct CanIf_Global CanIf_GlobalType;",
        req_annotations=[
            ReqAnnotation(
                canonical_id="SWS_CANIF_00101",
                raw="4.0.3/CANIF101",
                marker="@",
                claim="claimed_implemented",
                line=6,
            )
        ],
    )
    inner_struct_ref = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="CanIf_Global",
        kind="struct",
        line_span=(6, 6),
        text="struct CanIf_Global",
        req_annotations=[],
    )

    for unit in (outer_struct, typedef_unit, inner_struct_ref):
        db.upsert_code_unit(conn, unit)

    assert _get(conn, outer_struct) == outer_struct
    assert _get(conn, typedef_unit) == typedef_unit
    assert _get(conn, inner_struct_ref) == inner_struct_ref

    results = db.list_code_units_by_symbol(conn, "autosar-can", "CanIf_Global")
    assert len(results) == 3


def test_colliding_units_do_not_clobber_each_others_annotations(conn):
    """Round-tripping either of two same-symbol units must preserve its own
    req_annotations — proving neither upsert overwrote the other's evidence.
    """
    unit_a = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="Can_ReturnType",
        kind="typedef",
        line_span=(20, 20),
        req_annotations=[
            ReqAnnotation(
                canonical_id="SWS_CANIF_00050",
                raw="4.0.3/CANIF050",
                marker="@",
                claim="claimed_implemented",
                line=20,
            )
        ],
    )
    unit_b = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="Can_ReturnType",
        kind="enum",
        line_span=(21, 24),
        req_annotations=[
            ReqAnnotation(
                canonical_id="SWS_CANIF_00051",
                raw="4.0.3/CANIF051",
                marker="!",
                claim="claimed_not_implemented",
                line=22,
            )
        ],
    )

    db.upsert_code_unit(conn, unit_a)
    db.upsert_code_unit(conn, unit_b)

    fetched_a = _get(conn, unit_a)
    fetched_b = _get(conn, unit_b)
    assert fetched_a.claimed_implemented_ids == ["SWS_CANIF_00050"]
    assert fetched_b.claimed_implemented_ids == []
    assert [a.canonical_id for a in fetched_b.req_annotations] == ["SWS_CANIF_00051"]


def test_reupserting_same_colliding_units_is_idempotent(conn):
    """Re-running the same ingestion (same commit) must not duplicate rows."""
    struct_unit = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="Can_PduType",
        kind="struct",
        line_span=(10, 12),
    )
    typedef_unit = _code_unit(
        repo_path="communication/CanIf/src/CanIf.c",
        symbol="Can_PduType",
        kind="typedef",
        line_span=(10, 12),
    )

    for _ in range(2):
        db.upsert_code_unit(conn, struct_unit)
        db.upsert_code_unit(conn, typedef_unit)

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM code_units WHERE project_id = ? AND symbol = ?",
        ("autosar-can", "Can_PduType"),
    ).fetchone()["n"]
    assert count == 2


# --------------------------------------------------------------------------
# threads + messages
# --------------------------------------------------------------------------


def test_thread_create_list_get_rename_delete(conn):
    thread_id = db.create_thread(conn, "autosar-can", title="Initial title")

    fetched = db.get_thread(conn, thread_id)
    assert fetched["id"] == thread_id
    assert fetched["title"] == "Initial title"

    listed = db.list_threads(conn, "autosar-can")
    assert any(t["id"] == thread_id for t in listed)

    db.rename_thread(conn, thread_id, "Renamed")
    assert db.get_thread(conn, thread_id)["title"] == "Renamed"

    db.delete_thread(conn, thread_id)
    assert db.get_thread(conn, thread_id) is None


def test_message_create_list_get_with_parent_linkage(conn):
    thread_id = db.create_thread(conn, "autosar-can")

    root_id = db.create_message(
        conn, thread_id, "user", events=[{"type": "token", "data": "hi"}]
    )
    child_id = db.create_message(
        conn,
        thread_id,
        "assistant",
        events=[{"type": "done", "data": {}}],
        parent_message_id=root_id,
    )

    fetched_child = db.get_message(conn, child_id)
    assert fetched_child["parent_message_id"] == root_id
    assert fetched_child["events"] == [{"type": "done", "data": {}}]

    messages = db.list_messages(conn, thread_id)
    assert [m["id"] for m in messages] == [root_id, child_id]


def test_deleting_thread_cascades_to_messages(conn):
    thread_id = db.create_thread(conn, "autosar-can")
    message_id = db.create_message(conn, thread_id, "user", events=[])

    db.delete_thread(conn, thread_id)

    assert db.get_message(conn, message_id) is None


# --------------------------------------------------------------------------
# verdict_cache
# --------------------------------------------------------------------------


def test_verdict_cache_hit_on_same_triple(conn):
    verdict = {"status": "implemented", "evidence": [], "confidence": 0.9}
    db.put_verdict(conn, "SWS_CANIF_00023", GIT_SHA_A, "openai/gpt-4o-mini", verdict, "autosar-can")

    assert db.get_verdict(conn, "SWS_CANIF_00023", GIT_SHA_A, "openai/gpt-4o-mini") == verdict


def test_verdict_cache_miss_on_different_git_sha(conn):
    verdict = {"status": "implemented", "evidence": [], "confidence": 0.9}
    db.put_verdict(conn, "SWS_CANIF_00023", GIT_SHA_A, "openai/gpt-4o-mini", verdict, "autosar-can")

    assert db.get_verdict(conn, "SWS_CANIF_00023", GIT_SHA_B, "openai/gpt-4o-mini") is None


def test_verdict_cache_miss_on_different_model_id(conn):
    verdict = {"status": "implemented", "evidence": [], "confidence": 0.9}
    db.put_verdict(conn, "SWS_CANIF_00023", GIT_SHA_A, "openai/gpt-4o-mini", verdict, "autosar-can")

    assert db.get_verdict(conn, "SWS_CANIF_00023", GIT_SHA_A, "anthropic/claude-sonnet-4.5") is None


def test_verdict_cache_put_overwrites_same_triple(conn):
    db.put_verdict(
        conn,
        "SWS_CANIF_00023",
        GIT_SHA_A,
        "openai/gpt-4o-mini",
        {"status": "missing"},
        "autosar-can",
    )
    db.put_verdict(
        conn,
        "SWS_CANIF_00023",
        GIT_SHA_A,
        "openai/gpt-4o-mini",
        {"status": "implemented"},
        "autosar-can",
    )

    assert db.get_verdict(conn, "SWS_CANIF_00023", GIT_SHA_A, "openai/gpt-4o-mini") == {
        "status": "implemented"
    }


# --------------------------------------------------------------------------
# embedding_cache
# --------------------------------------------------------------------------


def test_embedding_cache_hit(conn):
    embedding = [0.1, 0.2, 0.3]
    db.put_embedding(conn, "hash-abc", "openai/text-embedding-3-small", embedding)

    assert db.get_embedding(conn, "hash-abc", "openai/text-embedding-3-small") == embedding


def test_embedding_cache_miss_on_different_hash(conn):
    db.put_embedding(conn, "hash-abc", "openai/text-embedding-3-small", [0.1, 0.2])

    assert db.get_embedding(conn, "hash-xyz", "openai/text-embedding-3-small") is None


def test_embedding_cache_miss_on_different_model(conn):
    db.put_embedding(conn, "hash-abc", "openai/text-embedding-3-small", [0.1, 0.2])

    assert db.get_embedding(conn, "hash-abc", "openai/text-embedding-3-large") is None
