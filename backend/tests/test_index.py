"""Tests for the Chroma collection, the BM25 index and the startup wiring.

Two things here look odd until you know why:

* The "no default embedding function" test is **behavioural** — it asserts that
  an ``add`` without ``embeddings=`` *raises*. Asserting on an attribute is not
  enough: chromadb 1.5.9 leaves ``Collection._embedding_function`` as ``None``
  and still falls through to the schema's local MiniLM, so only the behaviour
  distinguishes a safe collection from a poisoned one.
* The BM25 fixtures have **ten** documents, and the assertions are about
  *ranking order*, never about score values. Okapi's IDF is exactly ``0`` at
  ``N=2, n=1``, so a two-document fixture scores everything ``0.0`` and looks
  broken while being perfectly correct. See :mod:`retrieval.bm25`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import db
from core.manifest import load_manifest
from core.models import CodeUnit, ReqAnnotation, Requirement
from retrieval import bm25, vector_store
from retrieval.chunks import (
    CODE_DOC_TYPE,
    IndexDocument,
    code_chunk_id,
    code_document,
    module_for_path,
    module_names,
    requirement_chunk_id,
    requirement_document,
)
from retrieval.startup import load_indexes

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
PROJECT = MANIFEST.project_id
SHA = MANIFEST.code.git_sha


def requirement(
    req_id: str = "SWS_Can_00011",
    *,
    text: str = "The Can module shall reject a null pointer with CAN_E_PARAM_POINTER.",
    title: str | None = None,
    page: int = 32,
    doc_type: str = "requirement",
    upstream: list[str] | None = None,
    symbols: list[str] | None = None,
) -> Requirement:
    return Requirement(
        id=req_id,
        title=title,
        text=text,
        section_path="7.2 Can_Write",
        page=page,
        bbox=(71.0, 100.0, 524.0, 160.0),
        char_span=(10, 90),
        source_doc="AUTOSAR_CP_SWS_CANDriver.pdf",
        upstream_ids=upstream or [],
        named_symbols=symbols or [],
        version=MANIFEST.version,
        project_id=PROJECT,
        doc_type=doc_type,  # type: ignore[arg-type]
    )


def code_unit(
    symbol: str = "Can_Write",
    *,
    repo_path: str = "communication/CanIf/CanIf.c",
    kind: str = "function",
    line_span: tuple[int, int] = (120, 168),
    text: str | None = None,
    annotations: list[ReqAnnotation] | None = None,
) -> CodeUnit:
    return CodeUnit(
        repo_path=repo_path,
        language="c",
        symbol=symbol,
        kind=kind,  # type: ignore[arg-type]
        line_span=line_span,
        text=(
            text
            if text is not None
            else f"// {Path(repo_path).name} > {symbol}\nvoid {symbol}(void) {{}}"
        ),
        req_annotations=annotations or [],
        git_sha=SHA,
        project_id=PROJECT,
    )


# --------------------------------------------------------------------------
# chunk identity and metadata
# --------------------------------------------------------------------------


def test_chunk_ids_are_the_storage_key_and_nothing_positional():
    assert requirement_chunk_id(requirement()) == "req:SWS_Can_00011"
    unit = code_unit(kind="prototype", line_span=(10, 11))
    assert code_chunk_id(unit) == "code:communication/CanIf/CanIf.c:prototype:Can_Write:10-11"
    # kind is part of the key, so a struct and the typedef naming it differ.
    struct = code_unit(symbol="Can_PduType", kind="struct", line_span=(5, 9))
    typedef = code_unit(symbol="Can_PduType", kind="typedef", line_span=(5, 9))
    assert code_chunk_id(struct) != code_chunk_id(typedef)


def test_a_requirement_document_embeds_its_title_and_carries_filter_metadata():
    document = requirement_document(
        requirement(title="Can_Write shall reject null", upstream=["SRS_Can_01001"]),
        module="Can",
    )
    assert document.text.startswith("Can_Write shall reject null\n")
    metadata = document.metadata
    assert metadata["req_id"] == "SWS_Can_00011"
    assert metadata["doc_type"] == "requirement"
    assert metadata["source_doc"] == "AUTOSAR_CP_SWS_CANDriver.pdf"
    assert metadata["module"] == "Can"
    assert metadata["section_path"] == "7.2 Can_Write"
    assert metadata["page"] == 32
    assert metadata["upstream_ids"] == "SRS_Can_01001"


def test_absent_metadata_is_omitted_rather_than_stored_as_none():
    """Chroma rejects ``None``, and ``""`` is a filter value nobody wants."""
    document = requirement_document(requirement(title=None), module=None)
    assert "title" not in document.metadata
    assert "module" not in document.metadata
    assert "upstream_ids" not in document.metadata


def test_a_code_document_carries_repo_path_symbol_and_a_usable_line_span():
    document = code_document(code_unit(), module="CanIf")
    metadata = document.metadata
    assert metadata["doc_type"] == CODE_DOC_TYPE
    assert metadata["repo_path"] == "communication/CanIf/CanIf.c"
    assert metadata["symbol"] == "Can_Write"
    assert metadata["line_span"] == "120-168"
    assert (metadata["line_start"], metadata["line_end"]) == (120, 168)
    assert metadata["git_sha"] == SHA


def test_the_module_comes_from_the_manifests_own_module_names():
    modules = module_names(MANIFEST)
    assert module_for_path("communication/CanIf/CanIf.c", modules) == "CanIf"
    assert module_for_path("communication/CanTp/src/CanTp.c", modules) == "CanTp"
    assert module_for_path("vendor/lwip/api.c", modules) is None
    # The file's own name is never mistaken for a module directory.
    assert module_for_path("Com.c", modules) is None


def test_a_code_chunk_and_a_requirement_chunk_agree_on_the_module_spelling():
    """Two manifest fields name modules with different casing; one must win."""
    modules = module_names(MANIFEST)
    for document in MANIFEST.documents:
        assert (
            module_for_path(f"communication/{document.module}/x.c", modules) == document.module
        ), "code metadata must use the same spelling requirement metadata does"
    # A module with no ingested specification keeps its annotation-map spelling.
    assert module_for_path("communication/CanNm/CanNm.c", modules) == "CanNm"


# --------------------------------------------------------------------------
# Chroma
# --------------------------------------------------------------------------


@pytest.fixture
def collection(tmp_path: Path):
    client = vector_store.open_client(tmp_path / "chroma")
    return vector_store.open_collection(client, PROJECT)


def documents_and_vectors(count: int = 3) -> tuple[list[IndexDocument], list[list[float]]]:
    documents = [
        requirement_document(requirement(f"SWS_Can_{index:05d}", page=index + 1), module="Can")
        for index in range(count)
    ]
    vectors = [[float(index), 0.5, 0.25] for index in range(count)]
    return documents, vectors


def test_the_collection_has_no_default_embedding_function(collection):
    """The trap: without the configuration pin, Chroma embeds with a local MiniLM.

    Behavioural on purpose. ``_embedding_function`` is ``None`` either way;
    what separates a safe collection from a poisoned one is that this call
    fails instead of quietly producing 384-dimension vectors.
    """
    assert collection._embedding_function is None
    with pytest.raises(Exception) as error:
        collection.add(ids=["oops"], documents=["a chunk with no vector"])
    assert "embedding function" in str(error.value).lower()
    assert collection.count() == 0


def test_upsert_is_idempotent(collection):
    documents, vectors = documents_and_vectors()
    assert vector_store.upsert(collection, documents, vectors) == 3
    assert collection.count() == 3
    assert vector_store.upsert(collection, documents, vectors) == 3
    assert collection.count() == 3, "re-running must not duplicate a single chunk"


def test_metadata_and_text_round_trip(collection):
    documents, vectors = documents_and_vectors(1)
    vector_store.upsert(collection, documents, vectors)
    stored = collection.get(ids=[documents[0].id], include=["metadatas", "documents"])
    assert stored["documents"][0] == documents[0].text
    assert stored["metadatas"][0]["req_id"] == "SWS_Can_00000"
    assert stored["metadatas"][0]["module"] == "Can"
    assert stored["metadatas"][0]["page"] == 1


def test_a_smoke_query_returns_the_known_chunk(collection):
    documents, vectors = documents_and_vectors(5)
    vector_store.upsert(collection, documents, vectors)
    hits = vector_store.query(collection, vectors[3], limit=1)
    assert [hit.id for hit in hits] == [documents[3].id]
    assert hits[0].rank == 1
    assert hits[0].metadata["req_id"] == "SWS_Can_00003"


def test_a_query_can_filter_on_metadata(collection):
    requirement_doc = requirement_document(requirement("SWS_Can_00011"), module="Can")
    code_doc = code_document(code_unit(), module="CanIf")
    vector_store.upsert(collection, [requirement_doc, code_doc], [[1.0, 0.0, 0.0], [1.0, 0.0, 0.0]])
    hits = vector_store.query(
        collection, [1.0, 0.0, 0.0], limit=5, where={"doc_type": CODE_DOC_TYPE}
    )
    assert [hit.id for hit in hits] == [code_doc.id]


def test_a_length_mismatch_is_refused(collection):
    documents, vectors = documents_and_vectors(3)
    with pytest.raises(vector_store.VectorStoreError, match="refusing to pair"):
        vector_store.upsert(collection, documents, vectors[:2])


def test_mixed_dimensions_are_refused(collection):
    documents, _ = documents_and_vectors(2)
    with pytest.raises(vector_store.VectorStoreError, match="one vector space"):
        vector_store.upsert(collection, documents, [[1.0, 2.0, 3.0], [1.0, 2.0]])


def test_an_empty_vector_is_refused(collection):
    documents, _ = documents_and_vectors(1)
    with pytest.raises(vector_store.VectorStoreError):
        vector_store.upsert(collection, documents, [[]])


def test_upserting_nothing_is_a_no_op(collection):
    assert vector_store.upsert(collection, [], []) == 0


def test_reopening_the_store_keeps_both_the_data_and_the_pin(tmp_path: Path):
    path = tmp_path / "chroma"
    documents, vectors = documents_and_vectors(2)
    first = vector_store.open_collection(vector_store.open_client(path), PROJECT)
    vector_store.upsert(first, documents, vectors)

    reopened = vector_store.open_collection(vector_store.open_client(path), PROJECT)
    assert reopened.count() == 2
    with pytest.raises(Exception) as error:
        reopened.add(ids=["oops"], documents=["still no vector"])
    assert "embedding function" in str(error.value).lower()


# --------------------------------------------------------------------------
# BM25 tokenization
# --------------------------------------------------------------------------


def test_tokenize_keeps_the_whole_identifier_and_emits_its_parts():
    assert bm25.tokenize("Can_Write") == ["can_write", "can", "write"]
    assert bm25.tokenize("CAN_E_PARAM_POINTER") == [
        "can_e_param_pointer",
        "can",
        "e",
        "param",
        "pointer",
    ]


def test_tokenize_splits_bracketed_and_punctuated_tokens():
    assert bm25.tokenize("ST[11]") == ["st", "11"]
    assert bm25.tokenize("CanIf.c > CanIf_Transmit") == [
        "canif",
        "c",
        "canif_transmit",
        "canif",
        "transmit",
    ]


def test_tokenize_does_not_split_camel_case():
    """``CanIf`` must stay one token; splitting it yields ``can`` + noise."""
    assert bm25.tokenize("CanIf") == ["canif"]


# --------------------------------------------------------------------------
# BM25 ranking — ten documents, ranking assertions only
# --------------------------------------------------------------------------

#: Ten records, because Okapi IDF is exactly 0 at N=2 and every score would be
#: 0.0 on a smaller fixture. See the module docstring.
CORPUS = (
    ("req:SWS_Can_00011", "Can_Write shall accept a PDU and return E_OK.", "Can_Write"),
    ("req:SWS_Can_00012", "The Can module shall reject CAN_E_PARAM_POINTER.", None),
    ("req:SWS_Can_00013", "Can_SetControllerMode changes the controller state.", None),
    ("req:SWS_CANIF_00001", "CanIf_Transmit forwards a request to the driver.", None),
    ("req:SWS_CANIF_00002", "CanIf_RxIndication notifies the upper layer.", None),
    ("req:SWS_CanTp_00003", "The segmented transmission uses ST[11] as the index.", None),
    ("req:SWS_CanSM_00004", "The state machine shall request a bus-off recovery.", None),
    ("req:SWS_CanSM_00005", "Det_ReportError is called on a development error.", None),
    ("code:x.c:function:Can_Init:1-9", "// x.c > Can_Init\nvoid Can_Init(void) {}", "Can_Init"),
    (
        "code:y.c:function:Handler:1-9",
        "// y.c > Handler\nvoid Handler(void) { Can_Write(pdu); }",
        "Handler",
    ),
)


def build_corpus() -> bm25.Bm25Index:
    return bm25.build_index(
        [
            bm25.Bm25Record(
                id=chunk_id,
                text=text,
                doc_type=CODE_DOC_TYPE if chunk_id.startswith("code:") else "requirement",
                source="fixture",
                symbol=symbol,
            )
            for chunk_id, text, symbol in CORPUS
        ]
    )


def test_bm25_ranks_the_requirement_that_names_can_write_first():
    hits = bm25.search(build_corpus(), "Can_Write", limit=5)
    assert hits, "a ten-document corpus must produce non-zero scores"
    assert hits[0].record.id == "req:SWS_Can_00011"
    assert hits[0].rank == 1
    # The caller of Can_Write is found too, just lower.
    assert "code:y.c:function:Handler:1-9" in [hit.record.id for hit in hits]


def test_bm25_finds_an_error_constant_by_its_exact_token():
    hits = bm25.search(build_corpus(), "CAN_E_PARAM_POINTER", limit=3)
    assert hits[0].record.id == "req:SWS_Can_00012"


def test_bm25_finds_a_bracketed_token():
    hits = bm25.search(build_corpus(), "ST[11]", limit=3)
    assert hits[0].record.id == "req:SWS_CanTp_00003"


def test_bm25_ranks_the_definition_above_the_caller_via_its_symbol():
    hits = bm25.search(build_corpus(), "Can_Init", limit=3)
    assert hits[0].record.id == "code:x.c:function:Can_Init:1-9"


def test_search_is_pure():
    index = build_corpus()
    first = [(hit.record.id, hit.score) for hit in bm25.search(index, "Can_Write", limit=5)]
    second = [(hit.record.id, hit.score) for hit in bm25.search(index, "Can_Write", limit=5)]
    assert first == second
    # Another query in between must not disturb the first one's ranking.
    bm25.search(index, "Det_ReportError", limit=5)
    third = [(hit.record.id, hit.score) for hit in bm25.search(index, "Can_Write", limit=5)]
    assert first == third


def test_a_query_matching_nothing_returns_no_hits():
    assert bm25.search(build_corpus(), "quantum entanglement", limit=5) == []
    assert bm25.search(build_corpus(), "   ", limit=5) == []


def test_an_empty_corpus_is_searchable_and_returns_nothing():
    index = bm25.build_index([])
    assert len(index) == 0
    assert bm25.search(index, "Can_Write") == []


def test_a_predicate_narrows_the_result_without_changing_the_index():
    index = build_corpus()
    hits = bm25.search(
        index, "Can_Write", limit=5, predicate=lambda record: record.doc_type == CODE_DOC_TYPE
    )
    assert [hit.record.id for hit in hits] == ["code:y.c:function:Handler:1-9"]
    assert bm25.search(index, "Can_Write", limit=5)[0].record.id == "req:SWS_Can_00011"


# --------------------------------------------------------------------------
# loading from SQLite, and the startup wiring
# --------------------------------------------------------------------------


@pytest.fixture
def populated(tmp_path: Path):
    path = tmp_path / "reqtrace.db"
    conn = db.connect(path)
    db.migrate(conn)
    db.bulk_insert_requirements(
        conn,
        [
            requirement("SWS_Can_00011", title="Can_Write rejects null"),
            requirement("CTX_can_driver_7.2_01", doc_type="context", text="Prose about Can_Write."),
        ],
    )
    db.upsert_code_unit(
        conn,
        code_unit(annotations=[
            ReqAnnotation(
                canonical_id="SWS_Can_00011",
                raw="@req 4.0.3/CAN011",
                marker="@",
                claim="claimed_implemented",
                line=121,
            )
        ]),
    )
    yield path, conn
    conn.close()


def test_load_records_produces_the_same_ids_the_vector_store_uses(populated):
    _, conn = populated
    records = bm25.load_records(conn, PROJECT)
    ids = {record.id for record in records}
    assert requirement_chunk_id(requirement("SWS_Can_00011")) in ids
    assert code_chunk_id(code_unit()) in ids
    assert len(records) == 3


def test_load_records_is_ordered_deterministically(populated):
    _, conn = populated
    first = [record.id for record in bm25.load_records(conn, PROJECT)]
    second = [record.id for record in bm25.load_records(conn, PROJECT)]
    assert first == second == sorted(first[:2]) + first[2:]


def test_the_index_built_from_sqlite_finds_a_known_chunk(populated):
    _, conn = populated
    index = bm25.build_from_sqlite(conn, PROJECT)
    hits = bm25.search(index, "Can_Write", limit=5)
    assert hits, "the smoke query must return something"
    assert {hit.record.doc_type for hit in hits} <= {"requirement", "context", CODE_DOC_TYPE}


def test_startup_loads_the_index_and_reports_its_own_duration(populated):
    path, _ = populated
    indexes = load_indexes(MANIFEST, path)
    assert indexes.ready
    assert indexes.records == 3
    assert indexes.error is None
    assert indexes.elapsed_seconds >= 0.0
    assert bm25.search(indexes.bm25, "Can_Write")


def test_startup_without_a_database_reports_how_to_build_one(tmp_path: Path):
    """A fresh clone has no ``data/``; the API must still boot."""
    indexes = load_indexes(MANIFEST, tmp_path / "absent.db")
    assert not indexes.ready
    assert indexes.records == 0
    assert "ingestion.run" in (indexes.error or "")
    assert bm25.search(indexes.bm25, "Can_Write") == []
