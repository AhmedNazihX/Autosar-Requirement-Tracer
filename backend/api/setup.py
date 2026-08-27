"""First-run status and guided ingestion (stories S3.6.1, S3.6.2).

This is the backend for the setup screen, and its whole purpose is that a fresh
clone must never show a stack trace. So:

* ``GET /setup/status`` answers "can this app work yet?" with the three things
  that can be missing — an index, an API key, a code snapshot — and counts for
  what is present. It works whether or not the corpus loaded, because
  :func:`api.deps.load_state` records failures rather than raising.
* ``POST /setup/ingest`` runs the WP1 pipeline as a background job.
* ``GET /setup/ingest/events`` streams its progress.

``ingestion.run.Reporter`` was written for this — its docstring says "story
S3.6.2 substitutes an SSE one" — so :class:`_QueueReporter` subclasses it rather
than the pipeline growing a second progress mechanism.

**One job at a time, deliberately.** Ingestion writes the database the running
API is reading; two concurrent runs would race on it for no benefit, so a second
request while one is running returns the job already in flight rather than
starting another.

**The API keeps serving during a run, but its index is stale.** Nothing here
swaps the loaded index in place — the running process indexed what it indexed at
boot. So a finished job reports ``restart_required``, which is honest, rather
than the setup screen claiming readiness the current process cannot deliver.
"""

from __future__ import annotations

import queue
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from api import deps, sse
from api.sse import HEARTBEAT_SECONDS
from core import paths
from core.config import get_settings
from core.manifest import ProjectManifest
from ingestion import run as ingestion_run
from ingestion.code_fetcher import repo_dir

router = APIRouter(prefix="/setup", tags=["setup"])

JobStatus = Literal["idle", "running", "succeeded", "failed"]


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------


class DocumentStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    module: str
    page_count: int
    requirements: int


class SetupStatus(BaseModel):
    """Everything the first-run screen needs to decide what to show."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    api_key_present: bool
    index_present: bool
    snapshot_present: bool
    project_id: str | None
    corpus_version: str | None
    #: The pinned code snapshot's SHA, from the manifest. Reported because the
    #: sidebar showed the *fixture's* SHA before this existed, which is only
    #: right for as long as the two happen to agree — and a citation is only
    #: true against the snapshot it was resolved on.
    git_sha: str | None
    requirements: int
    context_chunks: int
    code_units: int
    indexed_chunks: int
    documents: list[DocumentStatus]
    #: Why the corpus is unusable, in a sentence naming the fix. ``None`` when
    #: everything is in place.
    error: str | None
    ingest: JobView


@router.get("/status", response_model=SetupStatus)
def status(state: deps.AppState = Depends(deps.state_of)) -> SetupStatus:
    """Can the app work yet, and if not, what is missing?"""
    manifest = state.manifest
    counts = {"requirements": 0, "context": 0, "code_units": 0}
    documents: list[DocumentStatus] = []

    if state.pool is not None and manifest is not None:
        counts = _counts(state)
        per_document = _requirements_per_document(state)
        documents = [
            DocumentStatus(
                key=entry.key,
                title=entry.title,
                module=entry.module,
                page_count=state.page_counts.get(entry.key, 0),
                requirements=per_document.get(entry.key, 0),
            )
            for entry in manifest.documents
        ]

    return SetupStatus(
        ready=state.ready,
        api_key_present=bool((get_settings().openrouter_api_key or "").strip()),
        index_present=manifest is not None and paths.db_path(manifest).is_file(),
        snapshot_present=manifest is not None and repo_dir(manifest).is_dir(),
        project_id=manifest.project_id if manifest else None,
        corpus_version=manifest.version if manifest else None,
        git_sha=manifest.code.git_sha if manifest else None,
        requirements=counts["requirements"],
        context_chunks=counts["context"],
        code_units=counts["code_units"],
        indexed_chunks=state.records,
        documents=documents,
        error=state.error,
        ingest=REGISTRY.view(),
    )


def _counts(state: deps.AppState) -> dict[str, int]:
    project_id = state.manifest.project_id
    row = state.pool.execute(
        "SELECT "
        "(SELECT COUNT(*) FROM requirements WHERE project_id = ? AND doc_type = 'requirement') "
        "AS requirements, "
        "(SELECT COUNT(*) FROM requirements WHERE project_id = ? AND doc_type = 'context') "
        "AS context, "
        "(SELECT COUNT(*) FROM code_units WHERE project_id = ?) AS code_units",
        (project_id, project_id, project_id),
    ).fetchone()
    return {key: int(row[key]) for key in ("requirements", "context", "code_units")}


def _requirements_per_document(state: deps.AppState) -> dict[str, int]:
    return {
        row["source_doc"]: int(row["n"])
        for row in state.pool.execute(
            "SELECT source_doc, COUNT(*) AS n FROM requirements "
            "WHERE project_id = ? AND doc_type = 'requirement' GROUP BY source_doc",
            (state.manifest.project_id,),
        )
    }


# --------------------------------------------------------------------------
# the ingestion job
# --------------------------------------------------------------------------


class JobView(BaseModel):
    """The job as the setup screen sees it."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    status: JobStatus = "idle"
    lines: list[str] = []
    error: str | None = None
    #: True once a run has finished successfully. The running process is still
    #: serving the index it loaded at boot, so the screen must say so rather
    #: than claim readiness this process cannot deliver.
    restart_required: bool = False


@dataclass
class _Job:
    id: str
    status: JobStatus = "running"
    lines: list[str] = field(default_factory=list)
    error: str | None = None
    events: queue.Queue = field(default_factory=queue.Queue)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def emit(self, text: str) -> None:
        with self.lock:
            self.lines.append(text)
        self.events.put(text)

    def finish(self, *, error: str | None = None) -> None:
        with self.lock:
            self.status = "failed" if error else "succeeded"
            self.error = error
        self.events.put(None)

    def view(self) -> JobView:
        with self.lock:
            return JobView(
                id=self.id,
                status=self.status,
                lines=list(self.lines),
                error=self.error,
                restart_required=self.status == "succeeded",
            )


class _Registry:
    """The one in-process ingestion job. No Celery, no Redis (spec §5)."""

    def __init__(self) -> None:
        self._job: _Job | None = None
        self._lock = threading.Lock()

    def current(self) -> _Job | None:
        with self._lock:
            return self._job

    def start(self) -> tuple[_Job, bool]:
        """The running job, or a new one. ``True`` when this call started it."""
        with self._lock:
            if self._job is not None and self._job.status == "running":
                return self._job, False
            self._job = _Job(id=uuid.uuid4().hex)
            return self._job, True

    def view(self) -> JobView:
        job = self.current()
        return job.view() if job is not None else JobView()

    def reset(self) -> None:
        """Drop the recorded job. For tests."""
        with self._lock:
            self._job = None


#: Module-level because it models a process-wide fact: one database, one
#: ingestion at a time.
REGISTRY = _Registry()


class _QueueReporter(ingestion_run.Reporter):
    """An ``ingestion.run.Reporter`` that publishes to a job instead of stdout.

    Exactly what that class's docstring anticipated, so the pipeline needs no
    second progress mechanism.
    """

    def __init__(self, job: _Job) -> None:
        super().__init__(quiet=True)
        self._job = job

    def line(self, text: str = "") -> None:
        self._job.emit(text)


class StartIngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    #: False when a run was already in flight and this request joined it.
    started: bool


@router.post("/ingest", response_model=StartIngestResponse)
def start_ingest(
    background: BackgroundTasks,
    state: deps.AppState = Depends(deps.state_of),
) -> StartIngestResponse:
    """Start the WP1 ingestion pipeline, or report the run already in flight."""
    job, started = REGISTRY.start()
    if started:
        manifest = state.manifest
        skip_embeddings = not (get_settings().openrouter_api_key or "").strip()
        background.add_task(_run_job, job, manifest, skip_embeddings)
    return StartIngestResponse(job_id=job.id, status=job.view().status, started=started)


def _run_job(
    job: _Job, manifest: ProjectManifest | None, skip_embeddings: bool
) -> None:
    """Run ingestion, funnelling its output into ``job``. Never raises."""
    if manifest is None:
        job.emit("The project manifest could not be read, so there is nothing to ingest.")
        job.finish(error="no manifest")
        return
    if skip_embeddings:
        job.emit(
            "OPENROUTER_API_KEY is not set: building the registry and the keyword "
            "index, but no vectors. Set the key and re-run to enable semantic search."
        )
    try:
        summary = ingestion_run.run_ingestion(
            manifest,
            ingestion_run.Options(skip_embeddings=skip_embeddings),
            reporter=_QueueReporter(job),
        )
    except Exception as exc:  # noqa: BLE001 - a job records its failure
        job.emit(f"Ingestion failed: {exc}")
        job.finish(error=str(exc))
        return
    job.emit(
        f"Done: {sum(one.requirements for one in summary.documents)} requirement(s) "
        f"across {len(summary.documents)} document(s). Restart the API to serve them."
    )
    job.finish()


@router.get("/ingest/events")
def ingest_events() -> StreamingResponse:
    """Stream the running job's progress lines as SSE.

    Replays what the job has already emitted before following it, so a client
    that connects late — or reconnects — sees the whole run rather than
    whatever happens next.
    """
    job = REGISTRY.current()

    def body() -> Iterator[str]:
        if job is None:
            yield _frame("idle", "No ingestion has been started.")
            return
        for line in job.view().lines:
            yield _frame("line", line)
        if job.view().status != "running":
            yield _frame(job.view().status, job.view().error or "")
            return
        def final() -> str:
            view = job.view()
            return _frame(view.status, view.error or "")

        yield from sse.follow(
            job.events,
            item_frame=lambda item: _frame("line", item),
            final_frame=final,
            heartbeat=HEARTBEAT_SECONDS,
        )

    return StreamingResponse(
        body(),
        media_type=sse.SSE_MEDIA_TYPE,
        headers=sse.SSE_HEADERS,
    )


def _frame(kind: str, text: str) -> str:
    """This stream's payload shape, on the shared envelope."""
    return sse.frame(kind, {"text": text})
