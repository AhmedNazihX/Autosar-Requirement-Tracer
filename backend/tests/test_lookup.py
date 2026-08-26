"""Tests for exact requirement lookup (story S2.1.1).

This is the one retrieval path that must **never** be semantic: a user who
types a requirement ID gets that requirement or a clean miss, never a
plausible neighbour. So the tests here are mostly about the two ways that
promise breaks.

**Sloppy input must still land exactly.** People type ``sws_can_11``, paste
``SWS_Can_00011`` out of a PDF, or write ``CANIF-23``. All three name one
requirement, and none of them is the spelling stored in the database.

**The document's own spelling is what comes back.** Finding B3: the CAN
Driver document spells 205 of its requirements ``SWS_Can_*`` and 7 of them
``SWS_CAN_*``, and the other documents disagree with both. Citations render
verbatim, so normalisation may only ever build the *probe* — never rewrite the
stored id.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import db
from core.models import Requirement
from retrieval.lookup import (
    MAX_ID_LENGTH,
    InvalidRequirementId,
    fingerprint,
    lookup,
)

PROJECT = "autosar-can"

#: Real ids from the ingested corpus, chosen for their spellings: the two
#: casings the CAN Driver uses for the same prefix, an all-caps module, a
#: mixed-case one, a ``CONSTR`` variant, and a 9xxxx number (which is *not*
#: zero-padded, so any rule that pads blindly to five digits breaks on it).
CORPUS_IDS = [
    "SWS_Can_00011",
    "SWS_CAN_00491",
    "SWS_Can_91025",
    "SWS_CANIF_00023",
    "SWS_CanTp_00079",
    "SWS_CanSM_00500",
    "SWS_CANIF_CONSTR_00001",
]


def _requirement(req_id: str, **overrides) -> Requirement:
    fields = {
        "id": req_id,
        "title": f"{req_id} title",
        "text": f"The module shall satisfy {req_id}.",
        "section_path": "7.6 L-PDU reception",
        "page": 42,
        "bbox": (10.0, 20.0, 100.0, 40.0),
        "source_doc": "can_driver",
        "named_symbols": ["Can_Write"],
        "version": "R23-11",
        "project_id": PROJECT,
    }
    fields.update(overrides)
    return Requirement(**fields)


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "lookup.db")
    db.migrate(connection)
    db.bulk_insert_requirements(connection, [_requirement(req_id) for req_id in CORPUS_IDS])
    # A context chunk, which lookup must never return: it is prose, not a
    # requirement, and it carries a synthetic id no citation should cite.
    db.upsert_requirement(
        connection,
        _requirement("CTX_can_driver_7_01", doc_type="context", title=None),
    )
    yield connection
    connection.close()


# --------------------------------------------------------------------------
# the fingerprint, as a pure function
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SWS_Can_00011", "CAN11"),
        ("sws_can_11", "CAN11"),
        ("SWS_CAN_00011", "CAN11"),
        ("can 11", "CAN11"),
        ("Can-11", "CAN11"),
        ("  SWS_Can_00011  ", "CAN11"),
        ("SWS_CANIF_00023", "CANIF23"),
        ("canif.23", "CANIF23"),
        ("SWS_CANIF_CONSTR_00001", "CANIFCONSTR1"),
        ("SWS_Can_91025", "CAN91025"),
    ],
)
def test_fingerprint_collapses_the_ways_one_id_gets_written(raw: str, expected: str):
    assert fingerprint(raw) == expected


def test_two_spellings_of_the_same_id_share_a_fingerprint():
    """Finding B3's casing split must not create two different requirements."""
    assert fingerprint("SWS_Can_00491") == fingerprint("SWS_CAN_00491")


def test_different_modules_do_not_collide():
    assert fingerprint("SWS_Can_00011") != fingerprint("SWS_CanTp_00011")
    assert fingerprint("SWS_CANIF_00001") != fingerprint("SWS_CANIF_CONSTR_00001")


def test_a_leading_zero_inside_the_number_is_not_significant():
    assert fingerprint("SWS_Can_00011") == fingerprint("SWS_Can_11")


# --------------------------------------------------------------------------
# malformed input is a bad request, not a miss
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "SWS_Can_",  # a module with no number
        "00011",  # a number with no module
        "_____",
        "!!!",
    ],
)
def test_input_that_cannot_name_a_requirement_is_rejected(conn, raw: str):
    """A 400, not a 404: the caller's input is wrong, not merely absent."""
    with pytest.raises(InvalidRequirementId):
        lookup(conn, PROJECT, raw)


def test_an_absurdly_long_id_is_rejected_before_it_reaches_sqlite(conn):
    """Story S6.3.1's length cap, enforced at the engine rather than the edge."""
    with pytest.raises(InvalidRequirementId, match=str(MAX_ID_LENGTH)):
        lookup(conn, PROJECT, "SWS_Can_" + "0" * (MAX_ID_LENGTH * 2))


def test_the_rejection_message_shows_what_was_passed(conn):
    with pytest.raises(InvalidRequirementId, match="not_an_id"):
        lookup(conn, PROJECT, "not_an_id")


def test_a_sql_shaped_string_is_refused_and_leaves_the_table_alone(conn):
    """Belt and braces: the query is parameterised, and this never reaches it."""
    with pytest.raises(InvalidRequirementId):
        lookup(conn, PROJECT, "'; DROP TABLE requirements; --")
    assert lookup(conn, PROJECT, "SWS_Can_00011") is not None


# --------------------------------------------------------------------------
# lookup
# --------------------------------------------------------------------------


def test_a_verbatim_id_resolves(conn):
    hit = lookup(conn, PROJECT, "SWS_Can_00011")
    assert hit is not None
    assert hit.requirement.id == "SWS_Can_00011"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sws_can_11", "SWS_Can_00011"),
        ("SWS_CAN_00011", "SWS_Can_00011"),
        ("can 11", "SWS_Can_00011"),
        ("Can-11", "SWS_Can_00011"),
        ("canif 23", "SWS_CANIF_00023"),
        ("cantp_79", "SWS_CanTp_00079"),
        ("cansm500", "SWS_CanSM_00500"),
        ("canif_constr_1", "SWS_CANIF_CONSTR_00001"),
        ("can 91025", "SWS_Can_91025"),
        ("sws_can_491", "SWS_CAN_00491"),
    ],
)
def test_a_sloppy_id_resolves_to_the_documents_own_spelling(conn, raw: str, expected: str):
    """The plan's headline example, plus every spelling the corpus really uses."""
    hit = lookup(conn, PROJECT, raw)
    assert hit is not None, f"{raw!r} should have resolved"
    assert hit.requirement.id == expected, "the stored spelling is what citations render"


def test_a_well_formed_id_that_does_not_exist_is_a_clean_miss(conn):
    assert lookup(conn, PROJECT, "SWS_Can_99999") is None


def test_lookup_never_returns_a_context_chunk(conn):
    """Prose is not a requirement, whatever its id looks like."""
    assert lookup(conn, PROJECT, "CTX_can_driver_7_01") is None


def test_lookup_is_scoped_to_the_project(conn):
    assert lookup(conn, "some-other-project", "SWS_Can_00011") is None


def test_lookup_is_not_semantic(conn):
    """A near-miss number must not resolve to its neighbour."""
    assert lookup(conn, PROJECT, "SWS_Can_00012") is None


# --------------------------------------------------------------------------
# the citation payload
# --------------------------------------------------------------------------


def test_the_citation_carries_what_the_pdf_pane_needs(conn):
    """Spec §6: ``{req_id, doc, page, bbox}`` drives the pdf.js highlight."""
    hit = lookup(conn, PROJECT, "sws_can_11")
    assert hit is not None

    assert hit.citation.req_id == "SWS_Can_00011"
    assert hit.citation.doc == "can_driver", "the manifest key, not a filename"
    assert hit.citation.page == 42
    assert hit.citation.bbox == (10.0, 20.0, 100.0, 40.0)


def test_a_requirement_that_crosses_a_page_break_cites_without_a_box(conn):
    """6% of the corpus has no bbox; the citation must still be usable."""
    db.upsert_requirement(conn, _requirement("SWS_Can_00777", bbox=None, page=51))

    hit = lookup(conn, PROJECT, "sws_can_777")

    assert hit is not None
    assert hit.citation.page == 51
    assert hit.citation.bbox is None


def test_the_citation_id_is_the_stored_spelling_not_the_query(conn):
    assert lookup(conn, PROJECT, "SWS_CAN_00011").citation.req_id == "SWS_Can_00011"
