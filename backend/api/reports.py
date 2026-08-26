"""The traceability report API (spec §6, stories S4.2.1 and S4.3.1).

    POST   /reports              → {job_id, est_cost_usd, …}
    GET    /reports              → the runs this process knows about
    GET    /reports/{id}         → one run, with its matrix once finished
    GET    /reports/{id}/events  → SSE {done, total, current}
    GET    /reports/{id}/export  → md | csv | json
    DELETE /reports/{id}         → stop a run that is still going

**A job is a FastAPI BackgroundTask over an in-process registry** — spec §5
rules out Celery and Redis, and this application is deliberately local-only.
The shape is :mod:`api.setup`'s, which already does exactly this for
ingestion, with one difference: ingestion is a process-wide singleton (there
is one database to write), while reports are keyed by id because two scopes
can legitimately be judged at once.

**Two places hold a run, and they hold different things.** The registry holds
live progress, which is per-process state that means nothing after a restart.
``report_runs`` in SQLite holds the finished document, which someone will
export next week. Writing progress into SQLite would put a write between every
judged requirement and the browser for a number nobody reads afterwards.

**The estimate is computed synchronously, before the job starts**, because it
is what the user is deciding on. It costs no model calls — the candidate count
is SQL and the price comes from OpenRouter's public catalogue
(:mod:`core.pricing`).

**The SSE payload is new wire format.** ``frontend/lib/events.ts`` is the
authority on the *chat* contract and says do not fork it; there is no report
contract there yet, so the frames are defined here as Pydantic models and WP5
must match them rather than invent a second shape.
"""

from __future__ import annotations

import json
import queue
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from api import deps
from core import db, llm
from core.config import get_settings
from engines import report
from engines.report import EXPORT_FORMATS, MEDIA_TYPES, ReportResult, ReportScope, ScopeError

router = APIRouter(tags=["reports"])

RunStatus = Literal["running", "succeeded", "aborted", "failed", "cancelled"]

#: Keeps an idle SSE connection alive through the long quiet stretch while a
#: judge call is in flight. Same reason as ``api.setup.HEARTBEAT_SECONDS``.
HEARTBEAT_SECONDS = 15.0

#: Finished runs kept in the in-process registry. The result is in SQLite
#: either way; this only bounds how many live progress records are retained.
MAX_REMEMBERED_RUNS = 20

NOT_READY_MESSAGE = (
    "The corpus is not indexed yet, so there is nothing to report on. Run "
    "'uv run python -m ingestion.run ../projects/autosar-can/project.yaml' in "
    "backend/, then restart the API."
)


# --------------------------------------------------------------------------
# the wire
# --------------------------------------------------------------------------


class ProgressData(BaseModel):
    """Spec §6's ``{done, total, current}``."""

    model_config = ConfigDict(extra="forbid")

    done: int = Field(ge=0)
    total: int = Field(ge=0)
    current: str


class RunView(BaseModel):
    """A run as the report drawer sees it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    status: RunStatus
    scope: ReportScope
    scope_label: str
    done: int = 0
    total: int = 0
    current: str | None = None
    est_cost_usd: float | None = None
    est_basis: str = ""
    cost_usd: float = 0.0
    ceiling_usd: float = 0.0
    error: str | None = None
    result: ReportResult | None = None


class LaunchRequest(BaseModel):
    """``POST /reports``' body — a :class:`ReportScope`, verbatim."""

    model_config = ConfigDict(extra="forbid")

    scope: ReportScope = Field(default_factory=ReportScope)


class LaunchResponse(BaseModel):
    """Everything the launch dialog needs to say what it just started."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: RunStatus
    scope_label: str
    requirements: int
    to_judge: int
    cached: int
    est_cost_usd: float | None
    est_basis: str
    ceiling_usd: float
    #: Ids the scope named that this corpus does not contain. Reported rather
    #: than rejected: eleven good ids and one typo is eleven rows and a note.
    unknown_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------


@dataclass
class _Run:
    """One report job's live state. Guarded by its own lock."""

    id: str
    scope: ReportScope
    ceiling_usd: float
    est_cost_usd: float | None = None
    est_basis: str = ""
    total: int = 0
    done: int = 0
    current: str | None = None
    status: RunStatus = "running"
    error: str | None = None
    result: ReportResult | None = None
    events: queue.Queue = field(default_factory=queue.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancelled: threading.Event = field(default_factory=threading.Event)

    def progress(self, done: int, total: int, current: str) -> None:
        with self.lock:
            self.done, self.total, self.current = done, total, current
        self.events.put(ProgressData(done=done, total=total, current=current))

    def finish(self, *, status: RunStatus, result: ReportResult | None, error: str | None) -> None:
        with self.lock:
            self.status, self.result, self.error = status, result, error
        self.events.put(None)

    def view(self) -> RunView:
        with self.lock:
            return RunView(
                id=self.id,
                status=self.status,
                scope=self.scope,
                scope_label=self.scope.label(),
                done=self.done,
                total=self.total,
                current=self.current,
                est_cost_usd=self.est_cost_usd,
                est_basis=self.est_basis,
                cost_usd=self.result.cost_usd if self.result else 0.0,
                ceiling_usd=self.ceiling_usd,
                error=self.error,
                result=self.result,
            )


class _Registry:
    """The in-process job table. No Celery, no Redis (spec §5)."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}
        self._lock = threading.Lock()

    def add(self, run: _Run) -> None:
        with self._lock:
            self._runs[run.id] = run
            # Bound the live table. Finished runs survive in SQLite, which is
            # where an export reads from anyway.
            while len(self._runs) > MAX_REMEMBERED_RUNS:
                oldest = next(iter(self._runs))
                if self._runs[oldest].status == "running":
                    break
                del self._runs[oldest]

    def get(self, run_id: str) -> _Run | None:
        with self._lock:
            return self._runs.get(run_id)

    def all(self) -> list[_Run]:
        with self._lock:
            return list(self._runs.values())

    def cancel_all(self) -> None:
        """Stop every running job. Called at shutdown so uvicorn can exit."""
        for run in self.all():
            run.cancelled.set()

    def reset(self) -> None:
        """Drop every recorded run. For tests."""
        with self._lock:
            self._runs.clear()


REGISTRY = _Registry()


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------


@router.post("/reports", response_model=LaunchResponse)
def launch(
    body: LaunchRequest,
    background: BackgroundTasks,
    state: deps.AppState = Depends(deps.state_of),
) -> LaunchResponse:
    """Estimate the run, register it, and start it in the background."""
    if not state.ready:
        raise HTTPException(status_code=409, detail=NOT_READY_MESSAGE)

    scope = body.scope
    try:
        engine = deps.build_engine(state)
        judge = llm.chat_model(state.manifest, "judge")
    except Exception as exc:  # noqa: BLE001 - a missing key must read as a 409
        raise HTTPException(
            status_code=409, detail=f"A report cannot be started: {exc}"
        ) from exc

    try:
        requirements, unknown = report.resolve_scope(engine, scope)
    except ScopeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    estimate = report.estimate_cost(
        engine, judge.model_id, requirements, rejudge=scope.rejudge
    )
    ceiling = report.ceiling_usd(state.manifest, get_settings())

    run = _Run(
        id=uuid.uuid4().hex,
        scope=scope,
        ceiling_usd=ceiling,
        est_cost_usd=estimate.usd,
        est_basis=estimate.basis,
        total=len(requirements),
    )
    REGISTRY.add(run)
    db.create_report_run(
        state.pool,
        state.manifest.project_id,
        scope.model_dump(mode="json"),
        est_cost_usd=estimate.usd,
        run_id=run.id,
    )
    background.add_task(_execute, state, run)

    return LaunchResponse(
        job_id=run.id,
        status="running",
        scope_label=scope.label(),
        requirements=estimate.requirements,
        to_judge=estimate.to_judge,
        cached=estimate.cached,
        est_cost_usd=estimate.usd,
        est_basis=estimate.basis,
        ceiling_usd=ceiling,
        unknown_ids=unknown,
    )


def _execute(state: deps.AppState, run: _Run) -> None:
    """Run one report to completion. Never raises — a job records its failure.

    Builds its own :class:`~retrieval.pipeline.Engine`: this executes on a
    background thread, and ``core.db.pooled`` hands that thread its own SQLite
    connection (findings C10/C11 — a shared connection is not merely
    unsupported, it silently returns wrong rows).
    """
    try:
        engine = deps.build_engine(state)
        judge = llm.chat_model(state.manifest, "judge")
        result = report.run_report(
            engine,
            judge,
            run.scope,
            ceiling=run.ceiling_usd,
            on_progress=run.progress,
            should_stop=run.cancelled.is_set,
        )
    except Exception as exc:  # noqa: BLE001 - the job reports, it does not crash
        message = f"The report failed: {type(exc).__name__}: {exc}"
        run.finish(status="failed", result=None, error=message)
        _record(state, run.id, status="failed", result=None, cost=None)
        return

    status: RunStatus = "succeeded"
    if run.cancelled.is_set():
        status = "cancelled"
    elif result.aborted:
        status = "aborted"
    run.finish(status=status, result=result, error=result.abort_reason)
    _record(state, run.id, status=status, result=result, cost=result.cost_usd)


def _record(
    state: deps.AppState,
    run_id: str,
    *,
    status: str,
    result: ReportResult | None,
    cost: float | None,
) -> None:
    """Persist the finished run. Never raises: the client already has it."""
    if state.pool is None:
        return
    try:
        db.finish_report_run(
            state.pool,
            run_id,
            status=status,
            actual_cost_usd=cost,
            result=result.model_dump(mode="json") if result is not None else None,
        )
    except Exception:  # noqa: BLE001 - a failed write must not fail the run
        return


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


@router.get("/reports", response_model=list[RunView])
def list_runs(state: deps.AppState = Depends(deps.state_of)) -> list[RunView]:
    """The runs this process knows about, newest first.

    In-process only. A run from before a restart lives in ``report_runs`` and
    is reachable by id; listing those too would mix live progress with
    historical records in one list that means two things.
    """
    return [run.view() for run in reversed(REGISTRY.all())]


@router.get("/reports/{run_id}", response_model=RunView)
def get_run(run_id: str, state: deps.AppState = Depends(deps.state_of)) -> RunView:
    """One run — live if this process is running it, from SQLite if not."""
    return _view_or_404(state, run_id)


@router.delete("/reports/{run_id}", response_model=RunView)
def cancel_run(run_id: str, state: deps.AppState = Depends(deps.state_of)) -> RunView:
    """Stop a run that is still going.

    Not in spec §6, and added deliberately: a scope of 398 requirements spends
    real money for minutes, and the only alternative to a stop button is
    killing the server. The run stops between requirements and keeps every
    verdict it has already bought.
    """
    run = REGISTRY.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no report run {run_id!r} in this process")
    run.cancelled.set()
    return run.view()


@router.get("/reports/{run_id}/export", response_class=PlainTextResponse)
def export_run(
    run_id: str,
    fmt: str = Query(default="md"),
    state: deps.AppState = Depends(deps.state_of),
) -> PlainTextResponse:
    """The finished matrix as Markdown, CSV or JSON."""
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown format {fmt!r}; expected one of {', '.join(EXPORT_FORMATS)}",
        )
    view = _view_or_404(state, run_id)
    if view.result is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"report {run_id!r} is {view.status} and has no matrix to export yet"
                if view.status == "running"
                else f"report {run_id!r} produced no matrix ({view.error or view.status})"
            ),
        )
    return PlainTextResponse(
        report.export(view.result, fmt),
        media_type=MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="report-{run_id}.{fmt}"'},
    )


@router.get("/reports/{run_id}/events")
def run_events(run_id: str, state: deps.AppState = Depends(deps.state_of)) -> StreamingResponse:
    """Stream ``{done, total, current}`` until the run ends."""
    return StreamingResponse(
        event_body(REGISTRY.get(run_id), run_id, state),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


def event_body(run: _Run | None, run_id: str, state: deps.AppState) -> Iterator[str]:
    """The frames of one events stream, as a plain generator.

    Separate from the endpoint so a test can *step* it. Driving it through the
    test client cannot exercise the follow loop at all: ``TestClient`` is
    synchronous, so by the time a request is made the background run has
    already finished and the generator takes its early exit. Stepping the
    generator by hand is the only way to interleave "reader is waiting" with
    "worker judged another requirement", which is the case this loop exists
    for.

    Replays the current position before following, so a client that connects
    late — or reconnects — sees where the run is rather than waiting for the
    next requirement to be judged.
    """
    if run is None:
        stored = db.get_report_run(state.pool, run_id) if state.pool else None
        if stored is None:
            yield _frame("error", {"message": f"no report run {run_id!r}"})
        else:
            yield _frame("done", _terminal(stored["status"], stored.get("result")))
        return

    view = run.view()
    yield _frame(
        "progress",
        ProgressData(done=view.done, total=view.total, current=view.current or "").model_dump(),
    )
    if view.status != "running":
        yield _frame("done", _terminal(view.status, None, view))
        return

    while True:
        try:
            item = run.events.get(timeout=HEARTBEAT_SECONDS)
        except queue.Empty:
            yield ": keep-alive\n\n"
            continue
        if item is None:
            yield _frame("done", _terminal(run.view().status, None, run.view()))
            return
        yield _frame("progress", item.model_dump())


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _view_or_404(state: deps.AppState, run_id: str) -> RunView:
    """A run from the registry, or reconstructed from its stored row."""
    run = REGISTRY.get(run_id)
    if run is not None:
        return run.view()

    stored = db.get_report_run(state.pool, run_id) if state.pool is not None else None
    if stored is None:
        raise HTTPException(status_code=404, detail=f"no report run {run_id!r}")

    result = ReportResult.model_validate(stored["result"]) if stored["result"] else None
    scope = ReportScope.model_validate(stored["scope"])
    return RunView(
        id=stored["id"],
        status=stored["status"],
        scope=scope,
        scope_label=scope.label(),
        done=len(result.rows) if result else 0,
        total=result.coverage.total if result else 0,
        est_cost_usd=stored["est_cost_usd"],
        est_basis="",
        cost_usd=stored["actual_cost_usd"] or 0.0,
        ceiling_usd=result.ceiling_usd if result else 0.0,
        error=result.abort_reason if result else None,
        result=result,
    )


def _terminal(status: str, stored_result: dict | None, view: RunView | None = None) -> dict:
    """The payload of the stream's final frame."""
    payload: dict = {"status": status}
    if view is not None and view.result is not None:
        payload.update(
            {
                "judged": view.result.coverage.judged,
                "total": view.result.coverage.total,
                "cost_usd": view.result.cost_usd,
                "aborted": view.result.aborted,
                "reason": view.result.abort_reason,
            }
        )
    elif stored_result is not None:
        coverage = stored_result.get("coverage") or {}
        payload.update(
            {
                "judged": coverage.get("judged", 0),
                "total": coverage.get("total", 0),
                "cost_usd": stored_result.get("cost_usd", 0.0),
                "aborted": stored_result.get("aborted", False),
                "reason": stored_result.get("abort_reason"),
            }
        )
    return payload


def _frame(kind: str, data: dict) -> str:
    """One SSE frame, in the ``{type, data}`` envelope the chat stream uses."""
    return f"data: {json.dumps({'type': kind, 'data': data})}\n\n"
