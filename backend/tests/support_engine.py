"""A wired :class:`~retrieval.pipeline.Engine` over a miniature of the corpus.

Shared by every test that needs the retrieval stack standing up — the pipeline
integration tests, the agent's tools, and the chat endpoint.

The fixture is a *miniature* of the ingested corpus rather than an abstraction
of it: real module spellings, real section paths, real chunk-id formats, and
requirements *and* context prose *and* code units. Ten documents, because
BM25's Okapi IDF is exactly zero on a two-document corpus (finding C4), so a
smaller fixture would score everything 0.0 and look broken.

Embeddings are a deterministic bag-of-concepts vector, so a dense hit means
something a test can assert on — "this paraphrase found that requirement" —
rather than "Chroma sorted some floats".
"""

from __future__ import annotations

import math
from pathlib import Path

from core import db
from core.embeddings import EmbeddingBatch, EmbeddingClient
from core.llm import chat_model
from core.manifest import load_manifest
from core.models import CodeUnit, Requirement
from retrieval import bm25, pipeline, vector_store
from retrieval.chunks import code_document, requirement_document
from tests.support_llm import FakeOpenRouter

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
PROJECT = MANIFEST.project_id
SHA = MANIFEST.code.git_sha

#: Page counts the fixture claims for its documents, so a citation's
#: ``page_count`` is a real number rather than a zero.
PAGE_COUNTS = {"can_driver": 131, "can_interface": 228, "can_transport": 108}

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


def build_index(tmp_path: Path) -> dict:
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

    for key, count in PAGE_COUNTS.items():
        db.put_document_meta(conn, PROJECT, key, page_count=count)

    return {
        "conn": conn,
        "collection": collection,
        "index": bm25.build_from_sqlite(conn, MANIFEST),
        "embeddings": EmbeddingClient(model_id="test/embedding-v1", transport=transport),
    }


def build_engine(parts, *, translate_replies=(), rerank_replies=()) -> tuple:
    """An :class:`Engine` with scripted LLM replies.

    ``translate_replies`` are consumed in pipeline order: the expansion call
    first, then the self-query call.
    """
    translate_fake = FakeOpenRouter(*translate_replies)
    rerank_fake = FakeOpenRouter(*rerank_replies)
    built = pipeline.Engine(
        manifest=MANIFEST,
        conn=parts["conn"],
        collection=parts["collection"],
        bm25_index=parts["index"],
        embeddings=parts["embeddings"],
        translate_llm=chat_model(
            MANIFEST, "translate", api_key="sk-test", http_client=translate_fake.http_client()
        ),
        rerank_llm=chat_model(
            MANIFEST, "rerank", api_key="sk-test", http_client=rerank_fake.http_client()
        ),
    )
    return built, translate_fake, rerank_fake


