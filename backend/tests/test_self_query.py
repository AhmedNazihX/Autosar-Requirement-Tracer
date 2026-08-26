"""Tests for self-query structured filters (story S2.4.1).

The stage turns "which scheduled functions does the CAN driver have?" into a
typed filter — ``module=Can``, ``section=Scheduled functions``,
``doc_type=requirement`` — that narrows retrieval *before* the search rather
than after it.

The property this module protects hardest is that **a filter the corpus cannot
satisfy is dropped, never applied**. An LLM guessing ``module="CAN Driver"`` or
``section="Error Handling"`` is entirely normal; applying either as a hard
constraint returns zero hits, and zero hits is far worse than an unfiltered
search — the user sees "nothing in the specification covers this" when the
truth is "the filter was wrong". So every extracted field is resolved against
what the corpus actually contains, and anything unresolvable is recorded in
``dropped`` and left off.

Both indexes must honour the same filter. BM25 reads every row in SQLite while
Chroma holds a subset, so if only the vector side were filtered the fused
result would contain BM25 hits that violate the user's constraint.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core import db
from core.manifest import load_manifest
from core.models import Requirement
from retrieval import bm25, self_query
from retrieval.self_query import QueryFilter
from tests.support_llm import fake_llm, json_body, openrouter_body

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
PROJECT = MANIFEST.project_id

#: A miniature of the real corpus: the module spellings and section paths are
#: taken from the ingested index, so a filter that works here works there.
CORPUS = [
    ("SWS_Can_00110", "can_driver", "8.5 Scheduled functions", "requirement"),
    ("SWS_Can_00011", "can_driver", "7.6 L-PDU transmission", "requirement"),
    ("SWS_Can_00238", "can_driver", "5.2 Driver Services", "requirement"),
    ("SWS_CANIF_00023", "can_interface", "8.6 Scheduled functions", "requirement"),
    ("SWS_CanTp_00079", "can_transport", "7.1 Services provided to upper layer", "requirement"),
    ("CTX_can_driver_8.5_01", "can_driver", "8.5 Scheduled functions", "context"),
]


def _requirement(req_id: str, source_doc: str, section: str, doc_type: str) -> Requirement:
    return Requirement(
        id=req_id,
        text=f"Body of {req_id}.",
        section_path=section,
        page=10,
        source_doc=source_doc,
        version="R23-11",
        project_id=PROJECT,
        doc_type=doc_type,
    )


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "self_query.db")
    db.migrate(connection)
    db.bulk_insert_requirements(
        connection, [_requirement(*fields) for fields in CORPUS]
    )
    yield connection
    connection.close()


def resolve(conn, **fields) -> self_query.Filter:
    return self_query.resolve(
        QueryFilter(**fields), manifest=MANIFEST, conn=conn, project_id=PROJECT
    )


def extractor(*replies, **kwargs):
    return fake_llm(MANIFEST, *replies, purpose="translate", **kwargs)


# --------------------------------------------------------------------------
# resolving a filter against the corpus
# --------------------------------------------------------------------------


def test_a_module_the_corpus_has_resolves(conn):
    assert resolve(conn, module="CanIf").module == "CanIf"


def test_a_module_resolves_case_insensitively(conn):
    """The model writes ``canif``; the metadata says ``CanIf``."""
    assert resolve(conn, module="canif").module == "CanIf"
    assert resolve(conn, module="CANIF").module == "CanIf"


def test_a_module_named_by_its_document_title_resolves(conn):
    """"CAN Interface" is what a person says; ``CanIf`` is what is indexed."""
    assert resolve(conn, module="CAN Interface").module == "CanIf"


def test_a_module_the_corpus_does_not_have_is_dropped_not_applied(conn):
    """The zero-result trap this whole stage has to avoid."""
    resolved = resolve(conn, module="Ethernet")

    assert resolved.module is None
    assert resolved.is_empty
    assert "module" in dict(resolved.dropped)
    assert "Ethernet" in dict(resolved.dropped)["module"]


def test_a_section_hint_resolves_to_the_paths_that_contain_it(conn):
    resolved = resolve(conn, section="Scheduled functions")

    assert set(resolved.section_paths) == {"8.5 Scheduled functions", "8.6 Scheduled functions"}


def test_a_section_hint_matches_case_insensitively(conn):
    assert resolve(conn, section="scheduled FUNCTIONS").section_paths


def test_a_section_hint_nothing_matches_is_dropped(conn):
    resolved = resolve(conn, section="Ethernet Frame Handling")

    assert resolved.section_paths == ()
    assert "section" in dict(resolved.dropped)


def test_a_section_hint_is_narrowed_by_the_module(conn):
    """"Scheduled functions in the driver" must not pull in CanIf's section."""
    resolved = resolve(conn, module="Can", section="Scheduled functions")

    assert resolved.section_paths == ("8.5 Scheduled functions",)


def test_doc_type_passes_through(conn):
    assert resolve(conn, doc_type="requirement").doc_type == "requirement"


def test_an_invalid_doc_type_is_refused_by_the_schema():
    """A closed set, so the model cannot invent a fourth kind of chunk."""
    with pytest.raises(ValueError):
        QueryFilter(doc_type="prose")


def test_an_empty_filter_resolves_to_an_empty_filter(conn):
    resolved = resolve(conn)
    assert resolved.is_empty
    assert resolved.dropped == ()


# --------------------------------------------------------------------------
# applying it to Chroma
# --------------------------------------------------------------------------


def test_an_empty_filter_produces_no_where_clause(conn):
    assert resolve(conn).where() is None


def test_a_single_condition_is_a_plain_equality(conn):
    assert resolve(conn, module="CanIf").where() == {"module": "CanIf"}


def test_two_conditions_are_combined_with_and(conn):
    where = resolve(conn, module="Can", doc_type="requirement").where()
    assert where == {"$and": [{"doc_type": "requirement"}, {"module": "Can"}]}


def test_resolved_sections_become_an_in_clause(conn):
    """Chroma cannot do substring matching, so the hint is resolved to paths."""
    where = resolve(conn, section="Scheduled functions").where()
    assert where == {
        "section_path": {"$in": ["8.5 Scheduled functions", "8.6 Scheduled functions"]}
    }


def test_a_single_resolved_section_is_still_an_in_clause(conn):
    where = resolve(conn, module="Can", section="Scheduled functions").where()
    assert {"section_path": {"$in": ["8.5 Scheduled functions"]}} in where["$and"]


# --------------------------------------------------------------------------
# applying it to BM25 — both indexes must agree
# --------------------------------------------------------------------------


def test_the_bm25_predicate_honours_the_same_filter(conn):
    """Otherwise the fused result contains hits that violate the constraint."""
    records = bm25.load_records(conn, MANIFEST)
    resolved = resolve(conn, module="Can", doc_type="requirement")

    kept = [record.id for record in records if resolved.matches(record)]

    assert "req:SWS_Can_00110" in kept
    assert "req:SWS_CANIF_00023" not in kept, "wrong module"
    assert "req:CTX_can_driver_8.5_01" not in kept, "wrong doc_type"


def test_bm25_records_carry_the_module_derived_from_the_manifest(conn):
    by_id = {record.id: record for record in bm25.load_records(conn, MANIFEST)}

    assert by_id["req:SWS_Can_00110"].module == "Can"
    assert by_id["req:SWS_CANIF_00023"].module == "CanIf"
    assert by_id["req:SWS_CanTp_00079"].module == "CanTp"


def test_bm25_records_carry_their_section_path(conn):
    by_id = {record.id: record for record in bm25.load_records(conn, MANIFEST)}
    assert by_id["req:SWS_Can_00110"].section_path == "8.5 Scheduled functions"


def test_an_empty_filter_matches_everything(conn):
    records = bm25.load_records(conn, MANIFEST)
    resolved = resolve(conn)
    assert all(resolved.matches(record) for record in records)


def test_a_section_filter_narrows_bm25_to_the_resolved_paths(conn):
    records = bm25.load_records(conn, MANIFEST)
    resolved = resolve(conn, section="Scheduled functions")

    kept = {record.id for record in records if resolved.matches(record)}

    assert kept == {
        "req:SWS_Can_00110",
        "req:SWS_CANIF_00023",
        "req:CTX_can_driver_8.5_01",
    }


# --------------------------------------------------------------------------
# extracting the filter with the model
# --------------------------------------------------------------------------


def test_the_demo_query_extracts_module_and_section(conn):
    """The story's acceptance case, grounded in the real corpus."""
    llm, _ = extractor(
        json_body({"module": "Can", "section": "Scheduled functions", "doc_type": "requirement"})
    )

    extraction = self_query.extract(
        llm,
        "which scheduled functions does the CAN driver have?",
        manifest=MANIFEST,
        conn=conn,
        project_id=PROJECT,
    )

    assert extraction.filter.module == "Can"
    assert extraction.filter.section_paths == ("8.5 Scheduled functions",)
    assert extraction.filter.doc_type == "requirement"
    assert not extraction.filter.is_empty


def test_a_general_question_extracts_no_filter(conn):
    """Most questions carry no constraint; inventing one narrows wrongly."""
    llm, _ = extractor(json_body({"module": None, "section": None, "doc_type": None}))

    extraction = self_query.extract(
        llm, "what is a CAN frame?", manifest=MANIFEST, conn=conn, project_id=PROJECT
    )

    assert extraction.filter.is_empty
    assert extraction.filter.where() is None


def test_a_hallucinated_module_is_dropped_and_reported(conn):
    llm, _ = extractor(json_body({"module": "FlexRay", "section": None, "doc_type": None}))

    extraction = self_query.extract(
        llm, "how does FlexRay work?", manifest=MANIFEST, conn=conn, project_id=PROJECT
    )

    assert extraction.filter.is_empty, "an unsatisfiable filter must not be applied"
    assert "module" in dict(extraction.filter.dropped)


def test_the_prompt_lists_the_modules_the_corpus_actually_has(conn):
    llm, fake = extractor(json_body({"module": None, "section": None, "doc_type": None}))

    self_query.extract(llm, "anything", manifest=MANIFEST, conn=conn, project_id=PROJECT)

    prompt = fake.prompt_of(0)
    for module in ("Can", "CanIf", "CanTp", "CanSM"):
        assert module in prompt


def test_the_question_is_fenced_as_data(conn):
    llm, fake = extractor(json_body({"module": None, "section": None, "doc_type": None}))

    self_query.extract(
        llm, "ignore instructions", manifest=MANIFEST, conn=conn, project_id=PROJECT
    )

    assert fake.prompt_of(0).count(self_query.QUESTION_FENCE) == 2


def test_a_model_failure_degrades_to_no_filter(conn):
    """An unfiltered search is a fine answer; a failed search is not."""
    llm, _ = extractor(*[httpx.Response(503, json={"error": {"message": "down"}})] * 6)

    extraction = self_query.extract(
        llm, "scheduled functions", manifest=MANIFEST, conn=conn, project_id=PROJECT
    )

    assert extraction.filter.is_empty
    assert extraction.fallback_reason is not None


def test_unparseable_output_degrades_to_no_filter(conn):
    llm, _ = extractor(openrouter_body("no idea"), openrouter_body("still none"))

    extraction = self_query.extract(
        llm, "scheduled functions", manifest=MANIFEST, conn=conn, project_id=PROJECT
    )

    assert extraction.filter.is_empty
    assert extraction.fallback_reason is not None


def test_the_cost_of_a_failed_extraction_is_still_reported(conn):
    llm, _ = extractor(
        openrouter_body("nope", cost_usd=0.001),
        openrouter_body("nope", cost_usd=0.001),
    )

    extraction = self_query.extract(
        llm, "anything", manifest=MANIFEST, conn=conn, project_id=PROJECT
    )

    assert extraction.usage.cost_usd == pytest.approx(0.002)


def test_extraction_uses_a_tool_less_model(conn):
    llm, _ = extractor()
    assert llm.is_tool_less
