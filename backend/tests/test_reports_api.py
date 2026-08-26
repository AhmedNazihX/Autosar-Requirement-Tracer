"""Tests for the report API (stories S4.2.1, S4.2.2, S4.3.1).

``TestClient`` runs a ``BackgroundTask`` to completion before ``post()``
returns, so a launched report here is a *finished* report by the next line.
That is convenient for the endpoints and useless for the progress stream — the
follow loop would never execute — so the stream is stepped directly through
:func:`api.reports.event_body` instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import deps, reports
from core import db, llm, pricing
from engines.report import ReportScope
from tests.support_engine import MANIFEST, build_index, close_index
from tests.support_llm import FakeOpenRouter, json_body


def verdict(status="implemented", *, evidence_items=(), **kwargs):
    return json_body(
        {
            "status": status,
            "evidence": [{"candidate": n, "rationale": why} for n, why in evidence_items],
            "confidence": 0.8,
            "rationale": f"scripted {status}.",
        },
        **kwargs,
    )


@pytest.fixture(autouse=True)
def clean_registry():
    reports.REGISTRY.reset()
    pricing.reset()
    yield
    reports.REGISTRY.reset()
    pricing.reset()


@pytest.fixture
def wired(tmp_path: Path, monkeypatch):
    """An app whose judge and retrieval models are scripted fakes.

    ``core.llm.chat_model`` is patched rather than the endpoint: it is the one
    seam where production decides how a model reaches the network, so every
    layer below it runs unchanged.
    """
    parts = build_index(tmp_path)
    state = deps.AppState(
        manifest=MANIFEST,
        pool=parts["conn"],
        collection=parts["collection"],
        bm25_index=parts["index"],
        records=len(parts["index"]),
        page_counts=db.page_counts(parts["conn"], MANIFEST.project_id),
    )
    fakes: dict[str, FakeOpenRouter] = {}
    scripted: list = []

    real_chat_model = llm.chat_model

    def fake_chat_model(manifest, purpose, **kwargs):
        fake = FakeOpenRouter(*(scripted if purpose == "judge" else []))
        fakes[purpose] = fake
        return real_chat_model(
            manifest, purpose, api_key="sk-test", http_client=fake.http_client()
        )

    monkeypatch.setattr(llm, "chat_model", fake_chat_model)
    monkeypatch.setattr(
        deps,
        "build_engine",
        lambda st: __import__(
            "retrieval.pipeline", fromlist=["Engine"]
        ).Engine(
            manifest=MANIFEST,
            conn=parts["conn"],
            collection=parts["collection"],
            bm25_index=parts["index"],
            embeddings=parts["embeddings"],
            translate_llm=fake_chat_model(MANIFEST, "translate"),
            rerank_llm=fake_chat_model(MANIFEST, "rerank"),
        ),
    )
    monkeypatch.setattr(
        pricing,
        "price_of",
        lambda model_id, **kw: pricing.ModelPrice(
            model_id=model_id, prompt=1.5e-7, completion=6e-7
        ),
    )

    app = FastAPI()
    app.include_router(reports.router)
    app.state.reqtrace = state

    def script(*replies) -> None:
        scripted.clear()
        scripted.extend(replies)

    yield TestClient(app), script, state, fakes
    close_index(parts)


# --------------------------------------------------------------------------
# POST /reports
# --------------------------------------------------------------------------


def test_launching_a_report_returns_a_job_id_and_a_cost_estimate(wired):
    """Spec §6: ``POST /reports`` → ``{job_id, est_cost}``."""
    client, script, _, _ = wired
    script(*[verdict()] * 2)

    body = client.post("/reports", json={"scope": {"module": "CanIf"}}).json()

    assert body["job_id"]
    assert body["scope_label"] == "module CanIf"
    assert body["requirements"] == 2
    assert body["to_judge"] == 2
    assert body["est_cost_usd"] > 0
    assert "openrouter" in body["est_basis"]
    assert body["ceiling_usd"] > 0


def test_the_estimate_quotes_only_what_is_not_already_cached(wired):
    client, script, _, _ = wired
    script(*[verdict()] * 4)
    client.post("/reports", json={"scope": {"module": "CanIf"}})

    second = client.post("/reports", json={"scope": {"module": "CanIf"}}).json()

    assert second["cached"] == 2
    assert second["to_judge"] == 0
    assert second["est_cost_usd"] == 0.0


def test_an_unpriceable_model_launches_anyway_with_no_figure(wired, monkeypatch):
    client, script, _, _ = wired
    script(*[verdict()] * 2)
    monkeypatch.setattr(pricing, "price_of", lambda *a, **k: None)
    monkeypatch.setattr(pricing, "failure_reason", lambda: "ConnectError: offline")

    body = client.post("/reports", json={"scope": {"module": "CanIf"}}).json()

    assert body["est_cost_usd"] is None
    assert "ConnectError" in body["est_basis"]
    assert body["to_judge"] == 2


def test_an_unknown_module_is_a_400_naming_the_known_ones(wired):
    client, _, _, _ = wired

    response = client.post("/reports", json={"scope": {"module": "Ethernet"}})

    assert response.status_code == 400
    assert "CanIf" in response.json()["detail"]


def test_unknown_requirement_ids_are_reported_not_rejected(wired):
    client, script, _, _ = wired
    script(verdict())

    body = client.post(
        "/reports", json={"scope": {"req_ids": ["SWS_Can_00011", "SWS_Can_99999"]}}
    ).json()

    assert body["requirements"] == 1
    assert body["unknown_ids"] == ["SWS_Can_99999"]


def test_an_unindexed_corpus_is_a_409_naming_the_fix(tmp_path):
    app = FastAPI()
    app.include_router(reports.router)
    app.state.reqtrace = deps.AppState(error="no index")
    client = TestClient(app)

    response = client.post("/reports", json={"scope": {}})

    assert response.status_code == 409
    assert "ingestion.run" in response.json()["detail"]


# --------------------------------------------------------------------------
# GET /reports/{id}
# --------------------------------------------------------------------------


def test_a_finished_run_carries_its_matrix(wired):
    client, script, _, _ = wired
    script(
        verdict("implemented", evidence_items=[(1, "the definition does it.")]),
        verdict("missing"),
    )
    job_id = client.post("/reports", json={"scope": {"module": "CanIf"}}).json()["job_id"]

    body = client.get(f"/reports/{job_id}").json()

    assert body["status"] == "succeeded"
    assert body["result"]["coverage"]["implemented"] == 1
    assert body["result"]["coverage"]["missing"] == 1
    assert [row["req_id"] for row in body["result"]["rows"]] == [
        "SWS_CANIF_00023",
        "SWS_CANIF_00329",
    ]


def test_a_finished_run_is_persisted_and_readable_after_the_registry_forgets(wired):
    """A run outlives the process' memory of it — ``report_runs`` holds the
    document, the registry only holds live progress."""
    client, script, state, _ = wired
    script(*[verdict()] * 2)
    job_id = client.post("/reports", json={"scope": {"module": "CanIf"}}).json()["job_id"]

    reports.REGISTRY.reset()
    body = client.get(f"/reports/{job_id}").json()

    assert body["status"] == "succeeded"
    assert len(body["result"]["rows"]) == 2
    assert db.get_report_run(state.pool, job_id)["actual_cost_usd"] is not None


def test_an_unknown_run_is_a_404(wired):
    client, _, _, _ = wired
    assert client.get("/reports/nope").status_code == 404


def test_listing_shows_the_runs_this_process_started(wired):
    client, script, _, _ = wired
    script(*[verdict()] * 4)
    client.post("/reports", json={"scope": {"module": "CanIf"}})
    client.post("/reports", json={"scope": {"module": "CanTp"}})

    listed = client.get("/reports").json()

    assert [run["scope_label"] for run in listed] == ["module CanTp", "module CanIf"]


def test_a_run_that_hits_the_ceiling_is_recorded_as_aborted(wired, monkeypatch):
    client, script, _, _ = wired
    script(*[verdict(cost_usd=0.9)] * 8)
    monkeypatch.setenv("MAX_REPORT_COST_USD", "2.0")
    from core.config import get_settings

    get_settings.cache_clear()

    job_id = client.post("/reports", json={"scope": {}}).json()["job_id"]
    body = client.get(f"/reports/{job_id}").json()
    get_settings.cache_clear()

    assert body["status"] == "aborted"
    assert body["result"]["aborted"]
    assert "MAX_REPORT_COST_USD" in body["error"]
    assert len(body["result"]["rows"]) < 8


# --------------------------------------------------------------------------
# DELETE /reports/{id}
# --------------------------------------------------------------------------


def test_cancelling_a_run_marks_it_cancelled(wired):
    client, _, _, _ = wired
    run = reports._Run(id="live", scope=ReportScope(), ceiling_usd=2.0)
    reports.REGISTRY.add(run)

    body = client.delete("/reports/live").json()

    assert run.cancelled.is_set()
    assert body["id"] == "live"


def test_cancelling_an_unknown_run_is_a_404(wired):
    client, _, _, _ = wired
    assert client.delete("/reports/nope").status_code == 404


# --------------------------------------------------------------------------
# GET /reports/{id}/export
# --------------------------------------------------------------------------


@pytest.fixture
def finished(wired):
    client, script, state, _ = wired
    script(
        verdict("implemented", evidence_items=[(1, "the definition does it.")]),
        verdict("missing"),
    )
    job_id = client.post("/reports", json={"scope": {"module": "CanIf"}}).json()["job_id"]
    return client, job_id


def test_markdown_export_is_a_downloadable_document(finished):
    client, job_id = finished

    response = client.get(f"/reports/{job_id}/export", params={"fmt": "md"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert f"report-{job_id}.md" in response.headers["content-disposition"]
    assert "# Traceability report — module CanIf" in response.text


def test_csv_export_has_a_header_row_per_requirement(finished):
    client, job_id = finished

    text = client.get(f"/reports/{job_id}/export", params={"fmt": "csv"}).text

    assert text.splitlines()[0].startswith("req_id,")
    assert len(text.strip().splitlines()) == 3


def test_json_export_is_the_whole_result(finished):
    client, job_id = finished

    payload = json.loads(client.get(f"/reports/{job_id}/export", params={"fmt": "json"}).text)

    assert payload["scope_label"] == "module CanIf"
    assert payload["coverage"]["judged"] == 2


def test_an_unknown_export_format_is_a_400(finished):
    client, job_id = finished

    response = client.get(f"/reports/{job_id}/export", params={"fmt": "pdf"})

    assert response.status_code == 400
    assert "md" in response.json()["detail"]


def test_exporting_a_run_with_no_matrix_yet_is_a_409(wired):
    client, _, _, _ = wired
    reports.REGISTRY.add(reports._Run(id="live", scope=ReportScope(), ceiling_usd=2.0))

    response = client.get("/reports/live/export")

    assert response.status_code == 409
    assert "running" in response.json()["detail"]


# --------------------------------------------------------------------------
# GET /reports/{id}/events
# --------------------------------------------------------------------------


def frames(text: str) -> list[dict]:
    return [
        json.loads(block[len("data: ") :])
        for block in text.split("\n\n")
        if block.startswith("data: ")
    ]


def test_the_event_stream_follows_a_running_run(wired):
    """Spec §6's ``{done, total, current}``, interleaved with the worker.

    Stepped by hand rather than requested through the test client: that client
    is synchronous, so a background run is always already finished by the time
    a request reaches the endpoint, and the follow loop — the part that
    matters — would never execute.
    """
    _, _, state, _ = wired
    run = reports._Run(id="live", scope=ReportScope(module="CanIf"), ceiling_usd=2.0)
    reports.REGISTRY.add(run)
    stream = reports.event_body(run, "live", state)

    replayed = frames(next(stream))[0]
    run.progress(1, 2, "SWS_CANIF_00023")
    followed = frames(next(stream))[0]
    run.finish(status="succeeded", result=None, error=None)
    terminal = frames(next(stream))[0]

    assert replayed == {"type": "progress", "data": {"done": 0, "total": 0, "current": ""}}
    assert followed == {
        "type": "progress",
        "data": {"done": 1, "total": 2, "current": "SWS_CANIF_00023"},
    }
    assert terminal == {"type": "done", "data": {"status": "succeeded"}}
    with pytest.raises(StopIteration):
        next(stream)


def test_a_quiet_run_gets_a_keep_alive_rather_than_a_dropped_connection(wired, monkeypatch):
    """A judge call takes seconds and a proxy will drop a silent connection."""
    _, _, state, _ = wired
    monkeypatch.setattr(reports, "HEARTBEAT_SECONDS", 0.01)
    run = reports._Run(id="live", scope=ReportScope(), ceiling_usd=2.0)
    reports.REGISTRY.add(run)
    stream = reports.event_body(run, "live", state)

    next(stream)  # the replayed position
    assert next(stream) == ": keep-alive\n\n"




def test_the_event_stream_of_a_finished_run_ends_immediately(finished):
    client, job_id = finished

    sent = frames(client.get(f"/reports/{job_id}/events").text)

    assert sent[-1]["type"] == "done"
    assert sent[-1]["data"]["status"] == "succeeded"
    assert sent[-1]["data"]["judged"] == 2


def test_the_event_stream_of_a_forgotten_run_reads_from_storage(finished):
    client, job_id = finished
    reports.REGISTRY.reset()

    sent = frames(client.get(f"/reports/{job_id}/events").text)

    assert sent == [
        {
            "type": "done",
            "data": {
                "status": "succeeded",
                "judged": 2,
                "total": 2,
                "cost_usd": sent[0]["data"]["cost_usd"],
                "aborted": False,
                "reason": None,
            },
        }
    ]


def test_the_event_stream_of_an_unknown_run_says_so(wired):
    client, _, _, _ = wired

    sent = frames(client.get("/reports/nope/events").text)

    assert sent[0]["type"] == "error"
    assert "nope" in sent[0]["data"]["message"]
