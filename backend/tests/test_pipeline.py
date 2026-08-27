"""Integration tests for the assembled retrieval pipeline (story S2.6.1).

This is WP2's acceptance test: the whole of spec §4 — expand, filter, hybrid,
fuse, rerank — running over a real SQLite registry, a real Chroma collection
and a real BM25 index, with only the network faked.

The fixture is a miniature of the ingested corpus, not an abstraction of it:
real module spellings, real section paths, real chunk-id formats, requirements
*and* context prose *and* code units. Ten documents, because BM25's Okapi IDF
is exactly zero on a two-document corpus (finding C4) and a smaller fixture
would score everything 0.0 and look broken.

Three properties are what make this an *assembly* test rather than five unit
tests again:

* **The stages are wired in the right order and the right things reach them.**
  The self-query filter must constrain the retrieval, not be computed and
  dropped; the rerank must see the fused candidates, not the raw ones.
* **Every stage is loggable.** Story S2.6.1 exists so story S7.2 can evaluate
  each stage, so the stage log is asserted on directly — names, order, and the
  fact that costs are attributed per stage rather than lumped.
* **`search_requirements` never returns code and `search_code` only returns
  code**, whatever the extracted filter says.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core import db
from core.models import CodeUnit
from retrieval import bm25, pipeline, self_query, vector_store
from retrieval.self_query import QueryFilter
from tests.support_engine import (
    MANIFEST,
    PROJECT,
    SHA,
    build_engine,
    build_index,
    embed_text,
)
from tests.support_llm import json_body


@pytest.fixture
def engine(tmp_path: Path):
    """The shared fixture index — see ``tests/support_engine``."""
    parts = build_index(tmp_path)
    yield parts
    parts["conn"].close_all()


def build(parts, **kwargs):
    return build_engine(parts, **kwargs)

NO_FILTER = {"module": None, "doc_type": None, "section": None}


def ids_of(result) -> list[str]:
    return [item.requirement.id for item in result.results]


# --------------------------------------------------------------------------
# search_requirements end to end
# --------------------------------------------------------------------------


def test_the_full_pipeline_answers_a_paraphrased_question(engine):
    """Every stage doing its job: user words in, the right requirement out."""
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["CanIf_ControllerBusOff callback", "BUSOFF state detection"]}),
            json_body(NO_FILTER),
        ],
        # The reranker puts the two bus-off requirements first.
        rerank_replies=[json_body({"order": [1, 2, 3]})],
    )

    result = pipeline.search_requirements(built, "how does the driver report bus-off?", top_n=3)

    assert len(result.results) == 3
    assert "SWS_Can_00272" in ids_of(result), "the requirement that names the callback"
    assert all(item.rank == position for position, item in enumerate(result.results, start=1))


def test_no_code_chunk_ever_comes_back_from_search_requirements(engine):
    """Even though the code units share the collection and the BM25 index."""
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["CanIf_Transmit"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2, 3, 4, 5]})],
    )

    result = pipeline.search_requirements(built, "CanIf_Transmit", top_n=5)

    assert result.results, "the query must still match requirements"
    assert all(not item.requirement.id.startswith("code:") for item in result.results)


def test_each_result_carries_what_the_pdf_pane_needs(engine):
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=1)

    requirement = result.results[0].requirement
    assert requirement.source_doc in {document.key for document in MANIFEST.documents}
    assert requirement.page == 42
    assert requirement.bbox == (10.0, 20.0, 100.0, 40.0)


def test_each_result_records_which_index_found_it(engine):
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2]})],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=2)

    assert result.results[0].found_by, "provenance must survive fusion and reranking"
    assert set(result.results[0].found_by) <= {"bm25", "dense"}


# --------------------------------------------------------------------------
# the self-query filter really constrains retrieval
# --------------------------------------------------------------------------


def test_an_extracted_filter_narrows_the_results(engine):
    """The demo case: it must constrain the search, not be computed and dropped."""
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["main function polling"]}),
            json_body(
                {"module": "Can", "section": "Scheduled functions", "doc_type": "requirement"}
            ),
        ],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "which scheduled functions does Can have?")

    assert ids_of(result) == ["SWS_Can_00110"], "CanIf's own scheduled function is excluded"


def test_explicit_filters_skip_the_self_query_call(engine):
    """The tool signature is ``search_requirements(query, filters?)``."""
    built, translate_fake, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["filtering"]})],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(
        built, "identifier filtering", filters=QueryFilter(module="CanIf"), top_n=1
    )

    assert translate_fake.calls == 1, "expansion only — no filter extraction"
    assert ids_of(result) == ["SWS_CANIF_00329"]


def test_a_filter_naming_code_is_dropped_rather_than_returning_nothing(engine):
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["bus off"]}),
            json_body({"module": None, "doc_type": "code", "section": None}),
        ],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=1)

    assert result.results, "a code filter in a requirement search must not empty the result"


def test_a_filter_that_matches_nothing_falls_back_to_an_unfiltered_search(engine):
    """An over-narrow filter must degrade, the way every other stage does.

    Measured, not imagined. In the F7.2 RAGAS run, "Under what configuration
    is CanSM_SetBaudrate not provided?" made the self-query stage infer a
    *section* filter pointing at the document's chapter 10 configuration
    annex — because the question contains the word "configuration". The real
    requirement is in 8.3.7.2, so all eight index queries returned zero hits
    and the search reported that the corpus does not cover a requirement
    sitting in it.

    Returning nothing is the worst available outcome here: an unfiltered
    search is a slightly worse search, but an empty one makes the agent deny
    the corpus contains something it does.
    """
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["bus off"]}),
            # Every condition here is individually valid — that is the point.
            # "6 Requirements Tracing" is a real Can section, but it holds only
            # a context chunk, so the *conjunction* with doc_type=requirement
            # matches nothing. Existing validation checks each condition, never
            # the set of them together, which is how the real miss got through.
            json_body(
                {"module": "Can", "section": "Requirements Tracing", "doc_type": "requirement"}
            ),
        ],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "bus off handling", top_n=1)

    assert result.results, "an over-narrow filter emptied the result instead of degrading"


def test_the_unfiltered_retry_is_recorded_rather_than_silent(engine):
    """A search that quietly changed its own filter must say so.

    The stage log is what the UI's RAG chips and the F7.2 eval both read, so a
    silent retry would make an unfiltered search indistinguishable from a
    filtered one that happened to work.
    """
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["bus off"]}),
            json_body(
                {"module": "Can", "section": "Requirements Tracing", "doc_type": "requirement"}
            ),
        ],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "bus off handling", top_n=1)

    retrieve = next(stage for stage in result.stages if stage.name == "retrieve")
    assert retrieve.detail.get("dropped_filter") is True


def test_a_filter_the_caller_stated_is_never_dropped(engine):
    """The fallback is for a *model's guess*, not for any narrow filter.

    A caller who passes ``module="CanSM"`` has asked a question about CanSM,
    and "no CanSM requirement matches" is a true answer to it. Widening the
    search would answer a different question — and this is the tool signature
    the agent uses when a user names a module outright.
    """
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off"]})],
        rerank_replies=[],
    )

    result = pipeline.search_requirements(
        built, "bus off handling", filters=QueryFilter(module="CanSM"), top_n=1
    )

    assert result.results == [], "an explicit filter was silently widened"
    retrieve = next(stage for stage in result.stages if stage.name == "retrieve")
    assert not retrieve.detail.get("dropped_filter")


def test_a_filter_that_does_match_is_still_honoured(engine):
    """The fallback must not become "ignore filters when they are strict"."""
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["main function polling"]}),
            json_body(
                {"module": "Can", "section": "Scheduled functions", "doc_type": "requirement"}
            ),
        ],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "which scheduled functions does Can have?")

    assert ids_of(result) == ["SWS_Can_00110"], "a working filter was discarded"
    retrieve = next(stage for stage in result.stages if stage.name == "retrieve")
    assert not retrieve.detail.get("dropped_filter")


# --------------------------------------------------------------------------
# stage instrumentation (what story S7.2 will evaluate)
# --------------------------------------------------------------------------


def test_every_stage_is_logged_in_order(engine):
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off state"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2]})],
    )

    result = pipeline.search_requirements(built, "how is bus-off reported?", top_n=2)

    assert [stage.name for stage in result.stages] == [
        "expand",
        "self_query",
        "retrieve",
        "fuse",
        "hydrate",
        "rerank",
    ]


def test_the_stage_log_is_json_serialisable(engine):
    """It goes into SSE events and RAGAS runs, so it cannot hold objects."""
    import json

    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=1)

    json.dumps([stage.as_dict() for stage in result.stages])


def test_the_expand_stage_logs_the_queries_it_produced(engine):
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["BUSOFF state", "controller bus off"]}),
            json_body(NO_FILTER),
        ],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "bus off?", top_n=1)

    expand = next(stage for stage in result.stages if stage.name == "expand")
    assert expand.detail["queries"][0] == "bus off?"
    assert "BUSOFF state" in expand.detail["queries"]


def test_cost_is_attributed_per_stage_and_totalled(engine):
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["bus off"]}, cost_usd=0.001),
            json_body(NO_FILTER, cost_usd=0.002),
        ],
        rerank_replies=[json_body({"order": [1]}, cost_usd=0.004)],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=1)

    by_name = {stage.name: stage for stage in result.stages}
    assert by_name["expand"].cost_usd == pytest.approx(0.001)
    assert by_name["self_query"].cost_usd == pytest.approx(0.002)
    assert by_name["rerank"].cost_usd == pytest.approx(0.004)
    # The embedding calls are the retrieve stage's cost.
    assert by_name["retrieve"].cost_usd > 0
    assert result.usage.cost_usd == pytest.approx(
        sum(stage.cost_usd for stage in result.stages)
    )


def test_usage_is_reported_per_purpose(engine):
    """LlmUsage refuses to mix purposes, so the pipeline reports them apart."""
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["bus off"]}, cost_usd=0.001),
            json_body(NO_FILTER, cost_usd=0.002),
        ],
        rerank_replies=[json_body({"order": [1]}, cost_usd=0.004)],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=1)

    by_purpose = {usage.purpose: usage for usage in result.usage.llm}
    assert by_purpose["translate"].cost_usd == pytest.approx(0.003)
    assert by_purpose["rerank"].cost_usd == pytest.approx(0.004)
    assert result.usage.embedding.calls > 0


# --------------------------------------------------------------------------
# degrading
# --------------------------------------------------------------------------


def test_the_search_still_works_when_every_llm_stage_fails(engine):
    """The pipeline's headline robustness property, asserted end to end."""
    down = [httpx.Response(503, json={"error": {"message": "down"}})] * 12
    built, _, _ = build(engine, translate_replies=down, rerank_replies=down)

    result = pipeline.search_requirements(built, "bus off", top_n=3)

    assert result.results, "retrieval must survive all three LLM stages failing"
    by_name = {stage.name: stage for stage in result.stages}
    assert by_name["expand"].detail["fallback_reason"]
    assert by_name["self_query"].detail["fallback_reason"]
    assert by_name["rerank"].detail["fallback_reason"]


def test_an_off_domain_question_still_returns_the_nearest_chunks(engine):
    """Documenting real behaviour rather than wishing it away.

    Dense retrieval always returns its ``k`` nearest neighbours — there is no
    distance at which Chroma declines — so an off-domain question produces
    confident-looking irrelevant hits. This stage must not invent a similarity
    threshold to hide that: the number would be arbitrary and would also
    silently drop genuine answers phrased unusually. Refusing out-of-domain
    questions is the agent's job (story S3.4.1), which is why that story exists.
    """
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["quantum chromodynamics"]}),
            json_body(NO_FILTER),
        ],
        rerank_replies=[json_body({"order": [1, 2]})],
    )

    result = pipeline.search_requirements(built, "quantum chromodynamics", top_n=2)

    assert result.results, "retrieval returns candidates; judging them is not its job"


def test_an_empty_index_returns_no_results_not_an_error(engine, tmp_path: Path):
    """A fresh clone before ingestion — the API must still answer."""
    empty_conn = db.connect(tmp_path / "empty.db")
    db.migrate(empty_conn)
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[],
    )
    bare = pipeline.Engine(
        manifest=MANIFEST,
        conn=empty_conn,
        collection=vector_store.open_collection(
            vector_store.open_client(tmp_path / "empty-chroma"), PROJECT
        ),
        bm25_index=bm25.build_index([]),
        embeddings=built.embeddings,
        translate_llm=built.translate_llm,
        rerank_llm=built.rerank_llm,
    )

    result = pipeline.search_requirements(bare, "bus off", top_n=5)

    assert result.results == []
    empty_conn.close()


def test_a_blank_query_is_refused(engine):
    built, _, _ = build(engine)
    with pytest.raises(ValueError, match="empty"):
        pipeline.search_requirements(built, "   ")


# --------------------------------------------------------------------------
# search_code
# --------------------------------------------------------------------------


def test_an_exact_symbol_short_circuits_to_the_ast_index(engine):
    """Deterministic and free: no expansion, no embedding, no rerank."""
    built, translate_fake, rerank_fake = build(engine)

    result = pipeline.search_code(built, symbol="CanIf_ControllerBusOff")

    assert [item.unit.symbol for item in result.results] == ["CanIf_ControllerBusOff"]
    assert translate_fake.calls == 0
    assert rerank_fake.calls == 0
    assert result.usage.cost_usd == 0.0


def test_an_exact_symbol_returns_the_definition_before_the_declaration(engine):
    """A prototype is not an implementation (ingestion/code_indexer.py)."""
    built, _, _ = build(engine)

    result = pipeline.search_code(built, symbol="CanIf_Transmit")

    assert [item.unit.kind for item in result.results] == ["function", "prototype"]


def test_an_unknown_symbol_falls_through_to_hybrid_search(engine):
    built, _, _ = build(engine, rerank_replies=[json_body({"order": [1, 2]})])

    result = pipeline.search_code(built, symbol="CanIf_NoSuchThing", top_n=2)

    assert result.results, "an unknown symbol should still search semantically"
    assert all(isinstance(item.unit, CodeUnit) for item in result.results)


def test_search_code_returns_only_code(engine):
    built, _, _ = build(engine, rerank_replies=[json_body({"order": [1, 2, 3]})])

    result = pipeline.search_code(built, query="transmit a pdu", top_n=3)

    assert result.results
    assert all(item.unit.language == "c" for item in result.results)


def test_search_code_does_not_expand_the_query(engine):
    """Code queries are symbol-shaped; rewriting them adds cost, not recall."""
    built, translate_fake, _ = build(engine, rerank_replies=[json_body({"order": [1]})])

    pipeline.search_code(built, query="transmit", top_n=1)

    assert translate_fake.calls == 0


def test_search_code_logs_its_stages(engine):
    built, _, _ = build(engine, rerank_replies=[json_body({"order": [1]})])

    result = pipeline.search_code(built, query="transmit", top_n=1)

    assert [stage.name for stage in result.stages] == [
        "symbol",
        "retrieve",
        "fuse",
        "hydrate",
        "rerank",
    ]


def test_search_code_needs_a_query_or_a_symbol(engine):
    built, _, _ = build(engine)
    with pytest.raises(ValueError, match="query or a symbol"):
        pipeline.search_code(built)


def test_a_code_result_carries_the_file_and_line_span(engine):
    """What the code pane needs to scroll to the evidence (story S5.3.2)."""
    built, _, _ = build(engine)

    item = pipeline.search_code(built, symbol="CanIf_ControllerBusOff").results[0]

    assert item.unit.repo_path == "communication/CanIf/src/CanIf.c"
    assert item.unit.line_span == (300, 340)
    assert item.unit.git_sha == SHA


# --------------------------------------------------------------------------
# stale index entries
# --------------------------------------------------------------------------


def test_a_chunk_id_with_no_row_behind_it_is_skipped(engine):
    """An index left over from a previous build must not crash a search."""
    from retrieval.chunks import IndexDocument

    vector_store.upsert(
        engine["collection"],
        [
            IndexDocument(
                id="req:SWS_Can_99999",
                text="a bus off chunk whose row was deleted",
                metadata={"doc_type": "requirement", "module": "Can"},
            )
        ],
        [embed_text("busoff busoff busoff")],
    )
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1, 2]})],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=2)

    assert "SWS_Can_99999" not in ids_of(result)
    hydrate = next(stage for stage in result.stages if stage.name == "hydrate")
    assert hydrate.detail["unresolved"] >= 1


# --------------------------------------------------------------------------
# the naive baseline story S7.2 compares against
# --------------------------------------------------------------------------


def test_the_pipeline_can_run_as_a_naive_single_query_top_k_baseline(engine):
    """No expansion, no filter extraction, no rerank — and no LLM call at all."""
    built, translate_fake, rerank_fake = build(engine)

    result = pipeline.search_requirements(
        built, "bus off", rewrites=0, extract_filters=False, use_rerank=False, top_n=3
    )

    assert result.results
    assert translate_fake.calls == 0
    assert rerank_fake.calls == 0
    assert [stage.name for stage in result.stages] == ["retrieve", "fuse", "hydrate"]


# --------------------------------------------------------------------------
# a doc_type filter may narrow to requirements, never away from them
# --------------------------------------------------------------------------


def test_a_context_doc_type_is_dropped_in_a_requirement_search(engine):
    """Measured on the real corpus: this filter hid every normative answer.

    "How does the driver report bus-off?" is a how-does-it-work question, so
    the model quite reasonably extracted ``doc_type=context`` — and the search
    then returned five prose chunks and not one requirement. The same happened
    to "which scheduled functions does the CAN driver have?", which narrowed to
    a single context chunk and excluded SWS_Can_00110, the requirement that
    actually answers it.

    The asymmetry decides it: applying the filter hides the normative content
    this tool exists to find, while dropping it only widens the search — the
    prose chunks still rank well for a definitional question, as they did
    before the filter existed.
    """
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["bus off reporting"]}),
            json_body({"module": None, "doc_type": "context", "section": None}),
        ],
        rerank_replies=[json_body({"order": [1, 2, 3, 4, 5]})],
    )

    result = pipeline.search_requirements(built, "how does the driver report bus-off?", top_n=5)

    assert any(
        item.requirement.doc_type == "requirement" for item in result.results
    ), "a requirement search must not be filtered down to prose only"
    self_query_stage = next(s for s in result.stages if s.name == "self_query")
    assert "doc_type" in self_query_stage.detail["dropped"]


def test_a_requirement_doc_type_is_still_honoured(engine):
    """Narrowing *to* requirements is the tool's whole purpose."""
    built, _, _ = build(
        engine,
        translate_replies=[
            json_body({"queries": ["bus off"]}),
            json_body({"module": None, "doc_type": "requirement", "section": None}),
        ],
        rerank_replies=[json_body({"order": [1, 2, 3]})],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=3)

    assert result.results
    assert all(item.requirement.doc_type == "requirement" for item in result.results)


def test_the_self_query_prompt_does_not_invite_a_context_filter():
    """The prompt taught the model the wrong rule; it must not teach it again."""
    assert "how it works" not in self_query.SYSTEM_PROMPT
