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
    # The document list still comes from the manifest: the setup screen has to
    # say what WILL be fetched before anything exists. Counts are the index's,
    # so they are zero.
    assert [doc["key"] for doc in body["documents"]] == [
        entry.key for entry in MANIFEST.documents
    ]
    assert all(doc["requirements"] == 0 for doc in body["documents"])


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


# --------------------------------------------------------------------------
# reloading the state in place
# --------------------------------------------------------------------------


def test_reload_swaps_the_state_without_a_restart(indexed, monkeypatch):
    """The 'Open ReqTrace' button after ingestion: the boot load, on demand."""
    http, _, _ = indexed
    fresh = deps.AppState(error="fresh sentinel")
    monkeypatch.setattr(setup.deps, "load_state", lambda path: fresh)

    body = http.post("/setup/reload").json()

    assert body["error"] == "fresh sentinel"
    assert http.app.state.reqtrace is fresh, "requests must now see the new state"


def test_reload_refuses_while_ingestion_runs(indexed, monkeypatch):
    """Reloading mid-run would open a database the pipeline is writing."""
    http, _, _ = indexed
    monkeypatch.setattr(setup, "_run_job", lambda *args: None)
    http.post("/setup/ingest")

    assert http.post("/setup/reload").status_code == 409


# --------------------------------------------------------------------------
# pasting a key
# --------------------------------------------------------------------------


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch):
    """Point the endpoint at a scratch .env and keep settings caches clean."""
    target = tmp_path / ".env"
    monkeypatch.setattr(setup, "ENV_FILE", target)
    # setenv first so monkeypatch restores whatever the environment held even
    # though the endpoint writes os.environ directly.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-preexisting")
    yield target
    setup.get_settings.cache_clear()


def test_a_pasted_key_is_saved_and_takes_effect(indexed, env_file):
    http, _, _ = indexed

    response = http.post("/setup/key", json={"key": "  sk-or-v1-pasted  "})

    assert response.status_code == 200
    assert "sk-or" not in response.text, "the key must never be echoed back"
    assert "OPENROUTER_API_KEY=sk-or-v1-pasted" in env_file.read_text()
    assert http.get("/setup/status").json()["api_key_present"] is True


def test_a_pasted_key_replaces_the_existing_line(indexed, env_file):
    env_file.write_text("MAX_REPORT_COST_USD=2.0\nOPENROUTER_API_KEY=sk-or-old\n")

    indexed[0].post("/setup/key", json={"key": "sk-or-v1-new"})

    content = env_file.read_text()
    assert "MAX_REPORT_COST_USD=2.0" in content, "other settings are kept"
    assert content.count("OPENROUTER_API_KEY=") == 1
    assert "sk-or-v1-new" in content
    assert "sk-or-old" not in content


def test_a_blank_or_broken_key_is_refused(indexed, env_file):
    http, _, _ = indexed
    assert http.post("/setup/key", json={"key": "   "}).status_code == 400
    assert http.post("/setup/key", json={"key": "sk-or v1 spaced"}).status_code == 400
    assert not env_file.exists(), "a refused key writes nothing"


# --------------------------------------------------------------------------
# the step tracker (canvas artboard B)
# --------------------------------------------------------------------------


def test_the_reporter_publishes_steps_to_the_job():
    """Each ``stage()`` call advances the structured step list the setup
    screen's tracker renders — the previous step done, this one running."""
    job, _ = setup.REGISTRY.start()
    reporter = setup._QueueReporter(job)

    reporter.stage("docs", "fetching 7 PDF(s)")
    first = {step.key: step for step in job.view().steps}
    assert first["docs"].status == "running"
    assert first["docs"].detail == "fetching 7 PDF(s)"
    assert first["extract"].status == "pending"

    reporter.stage("extract", "parsing PDFs")
    second = {step.key: step for step in job.view().steps}
    assert second["docs"].status == "done"
    assert second["extract"].status == "running"
    assert [step.key for step in job.view().steps] == list(setup.ingestion_run.STAGES)
    assert all(step.label for step in job.view().steps)


def test_a_completed_stage_swaps_its_detail_for_the_result_summary():
    """`stage()` says what a step is about to do; `stage_done()` says what it
    produced — the tracker shows the latter once the step finishes."""
    job, _ = setup.REGISTRY.start()
    reporter = setup._QueueReporter(job)

    reporter.stage("docs", "fetching 7 PDF(s)")
    reporter.stage_done("docs", "7 PDFs · 48.2 MB · 7 checksum match(es)")

    docs = next(s for s in job.view().steps if s.key == "docs")
    assert docs.status == "done"
    assert docs.detail == "7 PDFs · 48.2 MB · 7 checksum match(es)"


def test_a_finished_job_marks_the_running_step_done():
    job, _ = setup.REGISTRY.start()
    setup._QueueReporter(job).stage("docs", "fetching")
    job.finish()
    assert {s.key: s.status for s in job.view().steps}["docs"] == "done"


def test_a_failed_job_marks_the_running_step_failed():
    job, _ = setup.REGISTRY.start()
    setup._QueueReporter(job).stage("docs", "fetching")
    job.finish(error="autosar.org refused the connection")
    assert {s.key: s.status for s in job.view().steps}["docs"] == "failed"


def test_the_job_reports_when_it_started():
    """The elapsed clock on the setup screen ticks from this timestamp."""
    job, _ = setup.REGISTRY.start()
    assert job.view().started_at


def test_the_events_stream_carries_step_frames(indexed):
    job, _ = setup.REGISTRY.start()
    reporter = setup._QueueReporter(job)
    reporter.stage("docs", "fetching")
    job.finish()

    parsed = frames(indexed[0].get("/setup/ingest/events").text)

    steps_frames = [f for f in parsed if f["type"] == "steps"]
    assert steps_frames, "the step tracker needs structured frames, not prose"
    steps = steps_frames[-1]["data"]["steps"]
    assert steps[0]["key"] == "docs"
    assert steps[0]["label"], "the label is the wire's, so both screens agree"


# --------------------------------------------------------------------------
# cancellation
# --------------------------------------------------------------------------


def test_the_cancel_endpoint_flags_the_running_job(indexed, monkeypatch):
    http, _, _ = indexed
    monkeypatch.setattr(setup, "_run_job", lambda *args: None)
    http.post("/setup/ingest")

    body = http.post("/setup/ingest/cancel").json()

    # Still "running" until the job notices at its next report — the endpoint
    # only raises the flag.
    assert body["status"] == "running"
    assert setup.REGISTRY.current().cancelled.is_set()


def test_cancelling_when_nothing_runs_is_a_no_op(indexed):
    assert indexed[0].post("/setup/ingest/cancel").json()["status"] == "idle"


def test_a_cancelled_run_stops_at_the_next_report_and_keeps_its_steps(
    indexed, monkeypatch
):
    """Cancellation is cooperative: the flag is checked at every reporter
    callback, so the run stops at the next line or stage, never mid-write."""
    job, _ = setup.REGISTRY.start()

    def fake_run(manifest, options, reporter=None):
        reporter.stage("docs", "fetching 7 PDF(s)")
        job.cancelled.set()
        reporter.stage("extract", "parsing")
        raise AssertionError("the run must stop at the first report after cancel")

    monkeypatch.setattr(setup.ingestion_run, "run_ingestion", fake_run)
    setup._run_job(job, MANIFEST, False)

    view = job.view()
    assert view.status == "cancelled"
    assert view.restart_required is False
    statuses = {s.key: s.status for s in view.steps}
    assert statuses["docs"] == "done", "finished steps are kept"
    assert any("re-run" in line.lower() for line in view.lines), (
        "the cancel message must say finished work is kept"
    )
