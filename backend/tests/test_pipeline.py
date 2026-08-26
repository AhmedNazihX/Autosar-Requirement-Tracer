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

import math
from pathlib import Path

import httpx
import pytest

from core import db
from core.embeddings import EmbeddingBatch, EmbeddingClient
from core.manifest import load_manifest
from core.models import CodeUnit, Requirement
from retrieval import bm25, pipeline, self_query, vector_store
from retrieval.chunks import code_document, requirement_document
from retrieval.self_query import QueryFilter
from tests.support_llm import FakeOpenRouter, json_body

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
PROJECT = MANIFEST.project_id
SHA = MANIFEST.code.git_sha

CONCEPTS = ("busoff", "transmit", "baudrate", "init", "schedule", "filter")


def embed_text(text: str) -> list[float]:
    """A deterministic bag-of-concepts vector, so dense hits are meaningful.

    The trailing constant dimension keeps the vector from ever being all
    zeros — cosine distance against a zero vector is undefined, and a query
    mentioning none of the concepts (an off-domain question) is a case the
    pipeline has to handle rather than a case to avoid in the fixture.
    """
    lowered = text.lower().replace("bus-off", "busoff").replace("bus off", "busoff")
    raw = [float(lowered.count(concept)) for concept in CONCEPTS] + [0.1]
    norm = math.sqrt(sum(value * value for value in raw)) or 1.0
    return [value / norm for value in raw]


def transport(texts, model_id: str) -> EmbeddingBatch:
    return EmbeddingBatch(
        vectors=[embed_text(text) for text in texts],
        prompt_tokens=len(texts),
        total_tokens=len(texts),
        cost_usd=1e-7 * len(texts),
    )


# (id, source_doc, section_path, doc_type, text)
REQUIREMENTS = [
    (
        "SWS_Can_00272",
        "can_driver",
        "7.4 Bus-off handling",
        "requirement",
        "The Can module shall call CanIf_ControllerBusOff on a bus off event.",
    ),
    (
        "SWS_Can_00020",
        "can_driver",
        "7.4 Bus-off handling",
        "requirement",
        "The driver shall detect the BUSOFF state of the controller.",
    ),
    (
        "SWS_Can_00011",
        "can_driver",
        "7.6 L-PDU transmission",
        "requirement",
        "Can_Write shall transmit the PDU and return E_OK.",
    ),
    (
        "SWS_Can_00110",
        "can_driver",
        "8.5 Scheduled functions",
        "requirement",
        "Can_MainFunction_Write shall schedule the polling of transmit confirmation.",
    ),
    (
        "SWS_Can_00255",
        "can_driver",
        "7.9 Baudrate",
        "requirement",
        "Can_SetBaudrate shall change the baudrate of the controller.",
    ),
    (
        "SWS_CANIF_00023",
        "can_interface",
        "8.6 Scheduled functions",
        "requirement",
        "CanIf_MainFunction shall schedule the transmit buffer handling.",
    ),
    (
        "SWS_CANIF_00329",
        "can_interface",
        "7.12 Software filtering",
        "requirement",
        "CanIf shall filter the received CAN identifier against the configured range.",
    ),
    (
        "SWS_CanTp_00079",
        "can_transport",
        "7.1 Services provided to upper layer",
        "requirement",
        "CanTp shall initialise the segmented transmission on request.",
    ),
    (
        "CTX_can_driver_7.4_01",
        "can_driver",
        "7.4 Bus-off handling",
        "context",
        "Bus off is a state the CAN controller enters after repeated transmit errors.",
    ),
    (
        "CTX_can_driver_6_01",
        "can_driver",
        "6 Requirements Tracing",
        "context",
        "Requirement Description Satisfied by [SWS_Can_00272] [SWS_Can_00020] bus off",
    ),
]

# (repo_path, symbol, kind, line_span, text)
CODE_UNITS = [
    (
        "communication/CanIf/src/CanIf.c",
        "CanIf_ControllerBusOff",
        "function",
        (300, 340),
        "// CanIf.c > CanIf_ControllerBusOff\nvoid CanIf_ControllerBusOff(uint8 c) { busoff(c); }",
    ),
    (
        "communication/CanIf/src/CanIf.c",
        "CanIf_Transmit",
        "function",
        (120, 168),
        "// CanIf.c > CanIf_Transmit\nStd_ReturnType CanIf_Transmit(void) { transmit(); }",
    ),
    (
        "communication/CanIf/inc/CanIf.h",
        "CanIf_Transmit",
        "prototype",
        (77, 77),
        "// CanIf.h > CanIf_Transmit (declaration)\nStd_ReturnType CanIf_Transmit(void);",
    ),
    (
        "communication/CanTp/src/CanTp.c",
        "CanTp_MainFunction",
        "function",
        (1705, 1747),
        "// CanTp.c > CanTp_MainFunction\nvoid CanTp_MainFunction(void) { schedule(); }",
    ),
]


def _requirement(req_id, source_doc, section, doc_type, text) -> Requirement:
    return Requirement(
        id=req_id,
        title=None,
        text=text,
        section_path=section,
        page=42,
        bbox=(10.0, 20.0, 100.0, 40.0),
        source_doc=source_doc,
        named_symbols=[],
        version=MANIFEST.version,
        project_id=PROJECT,
        doc_type=doc_type,
    )


def _code_unit(repo_path, symbol, kind, line_span, text) -> CodeUnit:
    return CodeUnit(
        repo_path=repo_path,
        language="c",
        symbol=symbol,
        kind=kind,
        line_span=line_span,
        text=text,
        req_annotations=[],
        git_sha=SHA,
        project_id=PROJECT,
    )


@pytest.fixture
def engine(tmp_path: Path):
    """A fully wired :class:`~retrieval.pipeline.Engine` over a fixture index.

    Built the way the real thing is: rows into SQLite, documents and vectors
    into Chroma, BM25 from SQLite. The LLM fakes are attached per test.
    """
    conn = db.connect(tmp_path / "reqtrace.db")
    db.migrate(conn)

    requirements = [_requirement(*fields) for fields in REQUIREMENTS]
    units = [_code_unit(*fields) for fields in CODE_UNITS]
    db.bulk_insert_requirements(conn, requirements)
    for unit in units:
        db.upsert_code_unit(conn, unit)

    modules = {document.key: document.module for document in MANIFEST.documents}
    documents = [
        requirement_document(requirement, module=modules.get(requirement.source_doc))
        for requirement in requirements
    ] + [
        code_document(unit, module="CanIf" if "CanIf" in unit.repo_path else "CanTp")
        for unit in units
    ]

    collection = vector_store.open_collection(
        vector_store.open_client(tmp_path / "chroma"), PROJECT
    )
    vector_store.upsert(collection, documents, [embed_text(d.text) for d in documents])

    yield {
        "conn": conn,
        "collection": collection,
        "index": bm25.build_from_sqlite(conn, MANIFEST),
        "embeddings": EmbeddingClient(model_id="test/embedding-v1", transport=transport),
    }
    conn.close()


def build(engine, *, translate_replies=(), rerank_replies=()) -> tuple:
    """An :class:`Engine` with scripted LLM replies.

    ``translate_replies`` are consumed in pipeline order: the expansion call
    first, then the self-query call.
    """
    from core.llm import chat_model

    translate_fake = FakeOpenRouter(*translate_replies)
    rerank_fake = FakeOpenRouter(*rerank_replies)
    built = pipeline.Engine(
        manifest=MANIFEST,
        conn=engine["conn"],
        collection=engine["collection"],
        bm25_index=engine["index"],
        embeddings=engine["embeddings"],
        translate_llm=chat_model(
            MANIFEST, "translate", api_key="sk-test", http_client=translate_fake.http_client()
        ),
        rerank_llm=chat_model(
            MANIFEST, "rerank", api_key="sk-test", http_client=rerank_fake.http_client()
        ),
    )
    return built, translate_fake, rerank_fake


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


def test_each_result_carries_a_citation_ready_for_the_pdf_pane(engine):
    built, _, _ = build(
        engine,
        translate_replies=[json_body({"queries": ["bus off"]}), json_body(NO_FILTER)],
        rerank_replies=[json_body({"order": [1]})],
    )

    result = pipeline.search_requirements(built, "bus off", top_n=1)

    citation = result.results[0].citation
    assert citation.req_id == result.results[0].requirement.id
    assert citation.doc in {document.key for document in MANIFEST.documents}
    assert citation.page == 42
    assert citation.bbox == (10.0, 20.0, 100.0, 40.0)


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
