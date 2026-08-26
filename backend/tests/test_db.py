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


def test_code_unit_upsert_and_get_round_trip(conn):
    unit = _code_unit()
    db.upsert_code_unit(conn, unit)

    fetched = db.get_code_unit(
        conn, "autosar-can", "communication/CanIf/src/CanIf.c", "CanIf_Transmit"
    )
    assert fetched == unit


def test_code_unit_preserves_annotation_polarity(conn):
    unit = _code_unit()
    db.upsert_code_unit(conn, unit)

    fetched = db.get_code_unit(
        conn, "autosar-can", "communication/CanIf/src/CanIf.c", "CanIf_Transmit"
    )
    assert fetched.claimed_implemented_ids == ["SWS_CANIF_00023"]


def test_list_code_units_by_symbol(conn):
    unit_a = _code_unit(repo_path="communication/CanIf/src/CanIf.c", symbol="CanIf_Transmit")
    unit_b = _code_unit(repo_path="communication/CanTp/src/CanTp.c", symbol="CanIf_Transmit")
    db.upsert_code_unit(conn, unit_a)
    db.upsert_code_unit(conn, unit_b)

    results = db.list_code_units_by_symbol(conn, "autosar-can", "CanIf_Transmit")
    assert {u.repo_path for u in results} == {unit_a.repo_path, unit_b.repo_path}


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
