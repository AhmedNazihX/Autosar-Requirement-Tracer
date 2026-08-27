"""Tests for the read endpoints behind the source pane (story S3.3.2).

The acceptance criterion names the miss cases — unknown ID, doc and path must
all be clean 404s — so most of this file is about those, plus the path handling,
which is the one security-sensitive part (spec §8).

The path assertions are deliberately about *outcomes* rather than about the
mechanism: a traversal attempt and an unknown file must be indistinguishable
from outside, because a different error for the traversal case would confirm to
a prober that the path escaped the snapshot.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import deps, documents
from core import db
from tests.support_engine import MANIFEST, build_index, close_index

INDEXED_FILE = "communication/CanIf/src/CanIf.c"


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    """An app over the fixture index, with a snapshot on disk to read from."""
    parts = build_index(tmp_path)

    # A snapshot containing the file the fixture index knows about.
    snapshot = tmp_path / "repo"
    source = snapshot / INDEXED_FILE
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n".join(f"line {n}" for n in range(1, 61)) + "\n", encoding="utf-8"
    )
    # A file that exists in the snapshot but was never indexed.
    unindexed = snapshot / "communication" / "CanIf" / "src" / "secret.c"
    unindexed.write_text("not indexed\n", encoding="utf-8")
    # A file outside the snapshot, the target a traversal would want.
    (tmp_path / "outside.txt").write_text("secret\n", encoding="utf-8")

    monkeypatch.setattr(documents, "repo_dir", lambda manifest: snapshot)

    # The PDF endpoint reads `data/docs/`, which is gitignored and absent on a
    # fresh clone — point it at a tmp directory holding one stand-in file.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "AUTOSAR_CP_SWS_CANInterface.pdf").write_bytes(b"%PDF-1.7\nstand-in\n")
    monkeypatch.setattr(documents, "docs_dir", lambda manifest: docs)

    state = deps.AppState(
        manifest=MANIFEST,
        pool=parts["conn"],
        collection=parts["collection"],
        bm25_index=parts["index"],
        records=len(parts["index"]),
        page_counts=db.page_counts(parts["conn"], MANIFEST.project_id),
    )
    app = FastAPI()
    app.include_router(documents.router)
    app.state.reqtrace = state

    yield TestClient(app), state, tmp_path
    close_index(parts)


# --------------------------------------------------------------------------
# GET /requirements/{req_id}
# --------------------------------------------------------------------------


def test_a_requirement_is_returned_with_its_citation_payload(client):
    http, _, _ = client
    body = http.get("/requirements/SWS_Can_00011").json()

    assert body["req_id"] == "SWS_Can_00011"
    assert body["doc"] == "can_driver"
    assert body["doc_title"] == "Specification of CAN Driver"
    assert body["page_count"] > 0
    assert body["text"]


def test_a_loosely_spelled_id_resolves(client):
    """Same engine as the tool, so the endpoint inherits its forgiveness."""
    http, _, _ = client
    assert http.get("/requirements/sws_can_11").json()["req_id"] == "SWS_Can_00011"


def test_an_unknown_requirement_is_a_clean_404(client):
    http, _, _ = client
    response = http.get("/requirements/SWS_Can_99999")

    assert response.status_code == 404
    assert "No requirement" in response.json()["detail"]
    assert "Traceback" not in response.text


def test_a_malformed_id_is_a_400_not_a_404(client):
    """A bad request and an absent thing are different answers."""
    http, _, _ = client
    assert http.get("/requirements/nonsense").status_code == 400


# --------------------------------------------------------------------------
# GET /documents/{doc}/view
# --------------------------------------------------------------------------


def test_a_document_view_reports_its_page_count(client):
    http, _, _ = client
    body = http.get("/documents/can_driver/view").json()

    assert body["doc"] == "can_driver"
    assert body["filename"].endswith(".pdf")
    assert body["page_count"] > 0
    assert body["page"] is None, "no highlight was asked for"


def test_a_highlight_returns_the_page_and_bbox(client):
    """Coordinates for the pdf.js overlay — never an image (spec §7)."""
    http, _, _ = client
    body = http.get("/documents/can_driver/view?highlight=sws_can_11").json()

    assert body["highlight"] == "SWS_Can_00011"
    assert body["page"] == 42
    assert body["bbox"] == [10.0, 20.0, 100.0, 40.0]


def test_an_unknown_document_is_a_404_that_lists_the_known_ones(client):
    http, _, _ = client
    response = http.get("/documents/nope/view")

    assert response.status_code == 404
    assert "can_driver" in response.json()["detail"]


def test_a_requirement_in_another_document_is_a_404(client):
    """Asking can_driver to highlight a CanIf requirement is a mistake, not a hit."""
    http, _, _ = client
    response = http.get("/documents/can_driver/view?highlight=SWS_CANIF_00329")

    assert response.status_code == 404
    assert "can_interface" in response.json()["detail"]


# --------------------------------------------------------------------------
# GET /code/{path}
# --------------------------------------------------------------------------


def test_an_indexed_file_is_served(client):
    http, _, _ = client
    body = http.get(f"/code/{INDEXED_FILE}").json()

    assert body["repo_path"] == INDEXED_FILE
    assert body["total_lines"] == 60
    assert body["first_line"] == 1
    assert body["git_sha"] == MANIFEST.code.git_sha
    assert body["text"].startswith("line 1")


def test_a_line_span_slices_the_file(client):
    """What the code pane scrolls to (story S5.3.2)."""
    http, _, _ = client
    body = http.get(f"/code/{INDEXED_FILE}?lines=10-12").json()

    assert (body["first_line"], body["last_line"]) == (10, 12)
    assert body["text"] == "line 10\nline 11\nline 12"


def test_a_span_past_the_end_is_clamped(client):
    http, _, _ = client
    body = http.get(f"/code/{INDEXED_FILE}?lines=55-9999").json()
    assert body["last_line"] == 60


def test_a_span_starting_past_the_end_is_refused(client):
    http, _, _ = client
    assert http.get(f"/code/{INDEXED_FILE}?lines=900-950").status_code == 416


def test_a_malformed_span_is_rejected_by_validation(client):
    http, _, _ = client
    assert http.get(f"/code/{INDEXED_FILE}?lines=abc").status_code == 422


def test_an_unknown_file_is_a_clean_404(client):
    http, _, _ = client
    response = http.get("/code/communication/CanIf/src/nope.c")

    assert response.status_code == 404
    assert "No indexed file" in response.json()["detail"]


def test_a_file_in_the_snapshot_that_was_never_indexed_is_refused(client):
    """The index is the allowlist: being on disk is not permission."""
    http, _, tmp_path = client
    assert (tmp_path / "repo" / "communication" / "CanIf" / "src" / "secret.c").is_file()

    response = http.get("/code/communication/CanIf/src/secret.c")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "attempt",
    [
        "../outside.txt",
        "../../etc/passwd",
        "communication/../../outside.txt",
        "communication/CanIf/src/../../../../outside.txt",
        "/etc/passwd",
        "%2e%2e/outside.txt",
        "..%2Foutside.txt",
    ],
)
def test_a_traversal_attempt_is_refused(client, attempt: str):
    http, _, _ = client
    response = http.get(f"/code/{attempt}")

    assert response.status_code in (404, 400), attempt
    assert "secret" not in response.text, "nothing outside the snapshot may leak"


def test_a_traversal_is_indistinguishable_from_an_unknown_file(client):
    """A distinct error would confirm to a prober that the path escaped."""
    http, _, _ = client
    escaped = http.get("/code/../outside.txt")
    unknown = http.get("/code/communication/CanIf/src/nope.c")

    assert escaped.status_code == unknown.status_code


def test_an_indexed_file_missing_from_the_snapshot_says_so(client):
    """A deleted data/repo is a re-ingest, not a mystery."""
    http, _, tmp_path = client
    (tmp_path / "repo" / INDEXED_FILE).unlink()

    response = http.get(f"/code/{INDEXED_FILE}")

    assert response.status_code == 404
    assert "re-run ingestion" in response.json()["detail"]


# --------------------------------------------------------------------------
# not indexed yet
# --------------------------------------------------------------------------


def test_every_read_endpoint_reports_an_unindexed_corpus_as_503():
    app = FastAPI()
    app.include_router(documents.router)
    app.state.reqtrace = deps.AppState(manifest=MANIFEST, error="no index at data/")
    http = TestClient(app)

    for path in (
        "/requirements/SWS_Can_00011",
        "/documents/can_driver/view",
        "/code/communication/CanIf/src/CanIf.c",
    ):
        response = http.get(path)
        assert response.status_code == 503, path
        assert "no index" in response.json()["detail"]


# --------------------------------------------------------------------------
# GET /documents/{doc}/file  (story S5.3.1)
# --------------------------------------------------------------------------


def test_the_pdf_is_served_inline_for_pdfjs(client):
    """Spec §7 needs the bytes; `/view` returns coordinates only."""
    http, _, _ = client

    response = http.get("/documents/can_interface/file")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    # Inline, not an attachment: pdf.js reads it, the browser must not offer
    # to save it.
    assert response.headers["content-disposition"].startswith("inline")
    assert response.content.startswith(b"%PDF")


def test_an_unknown_document_key_is_a_404_naming_the_known_ones(client):
    http, _, _ = client

    response = http.get("/documents/nope/file")

    assert response.status_code == 404
    assert "can_interface" in response.json()["detail"]


def test_a_document_whose_pdf_was_never_fetched_says_to_ingest(client):
    """`data/` is gitignored, so this is the fresh-clone case, not an error."""
    http, _, _ = client

    response = http.get("/documents/can_driver/file")

    assert response.status_code == 404
    assert "ingestion" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "attempt",
    ["../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "can_interface/../../../etc/passwd"],
)
def test_the_pdf_endpoint_has_no_path_to_traverse(client, attempt: str):
    """`doc` is a manifest key, not a path.

    The filename comes from the matched manifest entry, so a traversal cannot
    be expressed — this asserts the shape rather than a sanitiser, because
    there is no sanitiser to get wrong.
    """
    http, _, _ = client

    response = http.get(f"/documents/{attempt}/file")

    assert response.status_code == 404
    assert b"root:" not in response.content
