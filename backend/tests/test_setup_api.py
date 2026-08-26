"""Tests for the first-run status and guided ingestion (S3.6.1, S3.6.2).

The acceptance criterion is a sequence: a fresh ``data/`` reports missing, the
ingest job streams progress, and status then reports ready. So the tests walk
that path rather than checking fields in isolation.

The thing this endpoint exists to prevent is a fresh clone showing a stack
trace, so every assertion about a missing corpus is also an assertion that the
response is a *sentence naming the fix*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import deps, setup
from core import db
from tests.support_engine import MANIFEST, build_index


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is process-wide, so each test starts from idle."""
    setup.REGISTRY.reset()
    yield
    setup.REGISTRY.reset()


def app_for(state: deps.AppState) -> TestClient:
    app = FastAPI()
    app.include_router(setup.router)
    app.state.reqtrace = state
    return TestClient(app)


@pytest.fixture
def indexed(tmp_path: Path):
    parts = build_index(tmp_path)
    state = deps.AppState(
        manifest=MANIFEST,
        pool=parts["conn"],
        collection=parts["collection"],
        bm25_index=parts["index"],
        records=len(parts["index"]),
        page_counts=db.page_counts(parts["conn"], MANIFEST.project_id),
    )
    yield app_for(state), state, parts
    parts["conn"].close_all()


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


def test_an_indexed_corpus_reports_ready_with_counts(indexed):
    http, _, _ = indexed
    body = http.get("/setup/status").json()

    assert body["ready"] is True
    assert body["error"] is None
    assert body["requirements"] > 0
    assert body["code_units"] > 0
    assert body["indexed_chunks"] > 0
    assert body["project_id"] == MANIFEST.project_id
    assert body["corpus_version"] == MANIFEST.version


def test_status_reports_each_document_with_its_page_count(indexed):
    http, _, _ = indexed
    documents = http.get("/setup/status").json()["documents"]

    assert {d["key"] for d in documents} == {e.key for e in MANIFEST.documents}
    can_driver = next(d for d in documents if d["key"] == "can_driver")
    assert can_driver["page_count"] > 0
    assert can_driver["requirements"] > 0


def test_a_missing_index_says_how_to_build_it():
    """A fresh clone. The whole point: no stack trace, a runnable command."""
    state = deps.AppState(
        manifest=MANIFEST,
        error="no index at data/reqtrace.db — run 'uv run python -m ingestion.run ...'",
    )
    body = app_for(state).get("/setup/status").json()

    assert body["ready"] is False
    assert "ingestion.run" in body["error"]
    assert body["requirements"] == 0
    assert body["documents"] == []


def test_status_works_even_with_no_manifest():
    """An unreadable manifest must still produce an answer, not a 500."""
    body = app_for(deps.AppState(error="could not read the project manifest")).get(
        "/setup/status"
    )

    assert body.status_code == 200
    assert body.json()["project_id"] is None


def test_status_reports_whether_a_key_is_configured(indexed, monkeypatch):
    http, _, _ = indexed
    from core.config import Settings

    monkeypatch.setattr(setup, "get_settings", lambda: Settings(_env_file=None,
                                                               openrouter_api_key=None))
    assert http.get("/setup/status").json()["api_key_present"] is False

    monkeypatch.setattr(setup, "get_settings", lambda: Settings(_env_file=None,
                                                               openrouter_api_key="sk-or-x"))
    assert http.get("/setup/status").json()["api_key_present"] is True


def test_an_idle_registry_reports_idle(indexed):
    http, _, _ = indexed
    assert http.get("/setup/status").json()["ingest"]["status"] == "idle"


# --------------------------------------------------------------------------
# the ingest job
# --------------------------------------------------------------------------


def test_starting_a_job_reports_it_as_started(indexed, monkeypatch):
    http, _, _ = indexed
    monkeypatch.setattr(setup, "_run_job", lambda *args: None)

    body = http.post("/setup/ingest").json()

    assert body["started"] is True
    assert body["status"] == "running"
    assert body["job_id"]


def test_a_second_request_joins_the_run_already_in_flight(indexed, monkeypatch):
    """Two concurrent runs would race on the database the API is reading."""
    http, _, _ = indexed
    monkeypatch.setattr(setup, "_run_job", lambda *args: None)

    first = http.post("/setup/ingest").json()
    second = http.post("/setup/ingest").json()

    assert second["started"] is False
    assert second["job_id"] == first["job_id"]


def test_a_successful_job_says_a_restart_is_required(indexed):
    """The running process still serves the index it loaded at boot."""
    job, _ = setup.REGISTRY.start()
    job.emit("all done")
    job.finish()

    body = indexed[0].get("/setup/status").json()["ingest"]
    assert body["status"] == "succeeded"
    assert body["restart_required"] is True


def test_a_failed_job_records_its_reason(indexed):
    job, _ = setup.REGISTRY.start()
    job.finish(error="autosar.org refused the connection")

    body = indexed[0].get("/setup/status").json()["ingest"]
    assert body["status"] == "failed"
    assert "autosar.org" in body["error"]
    assert body["restart_required"] is False


def test_the_job_reports_a_pipeline_failure_rather_than_raising(indexed, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("PDF download failed")

    monkeypatch.setattr(setup.ingestion_run, "run_ingestion", boom)
    job, _ = setup.REGISTRY.start()

    setup._run_job(job, MANIFEST, False)

    view = job.view()
    assert view.status == "failed"
    assert "PDF download failed" in view.error


def test_a_job_without_a_manifest_fails_cleanly(indexed):
    job, _ = setup.REGISTRY.start()
    setup._run_job(job, None, False)

    assert job.view().status == "failed"
    assert "manifest" in job.view().lines[0]


def test_a_job_without_a_key_says_it_is_skipping_vectors(indexed, monkeypatch):
    """Story S1.5's --skip-embeddings path, surfaced to the setup screen."""
    monkeypatch.setattr(
        setup.ingestion_run, "run_ingestion",
        lambda *a, **k: _FakeSummary(),
    )
    job, _ = setup.REGISTRY.start()

    setup._run_job(job, MANIFEST, True)

    assert any("no vectors" in line for line in job.view().lines)
    assert job.view().status == "succeeded"


class _FakeSummary:
    documents = ()


# --------------------------------------------------------------------------
# progress events
# --------------------------------------------------------------------------


def frames(text: str) -> list[dict]:
    parsed = []
    for frame in text.split("\n\n"):
        payload = "\n".join(
            line[5:].strip() for line in frame.splitlines() if line.startswith("data:")
        )
        if payload:
            parsed.append(json.loads(payload))
    return parsed


def test_the_events_stream_replays_a_finished_job(indexed):
    """A client connecting late sees the whole run, not just what comes next."""
    job, _ = setup.REGISTRY.start()
    job.emit("[1/5] docs: fetching")
    job.emit("[2/5] extract: parsing")
    job.finish()

    parsed = frames(indexed[0].get("/setup/ingest/events").text)

    assert [f["data"]["text"] for f in parsed[:2]] == [
        "[1/5] docs: fetching",
        "[2/5] extract: parsing",
    ]
    assert parsed[-1]["type"] == "succeeded"


def test_the_events_stream_reports_a_failure_as_the_terminal_frame(indexed):
    job, _ = setup.REGISTRY.start()
    job.emit("[1/5] docs: fetching")
    job.finish(error="connection refused")

    parsed = frames(indexed[0].get("/setup/ingest/events").text)

    assert parsed[-1]["type"] == "failed"
    assert "connection refused" in parsed[-1]["data"]["text"]


def test_the_events_stream_says_so_when_nothing_has_been_started(indexed):
    parsed = frames(indexed[0].get("/setup/ingest/events").text)
    assert parsed == [{"type": "idle", "data": {"text": "No ingestion has been started."}}]


def test_the_reporter_publishes_pipeline_lines_to_the_job():
    """``ingestion.run.Reporter`` was written to be substituted like this."""
    job, _ = setup.REGISTRY.start()
    reporter = setup._QueueReporter(job)

    reporter.stage("docs", "fetching 4 document(s)")
    reporter.line("      wrote data/extraction_report.md")

    lines = job.view().lines
    assert lines[0].startswith("[1/5] docs:")
    assert "extraction_report" in lines[1]
