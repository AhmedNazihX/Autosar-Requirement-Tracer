"""FastAPI application entrypoint for the ReqTrace backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import chat, deps, documents
from core.config import get_settings

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Load the corpus before serving (spec §3).

    Never fatal, and that is enforced in :func:`api.deps.load_state` rather
    than promised here. A fresh clone has no ``data/`` yet; a database that is
    corrupt or WAL-locked (``make dev`` while ingestion runs) is just as
    survivable. Either way the API comes up with ``state.error`` explaining
    what to do — ``GET /setup/status`` needs the server alive in order to say
    so, and a process that died cannot.
    """
    app.state.reqtrace = deps.load_state(get_settings().project_manifest)
    try:
        yield
    finally:
        app.state.reqtrace.close()


app = FastAPI(title="ReqTrace API", version=__version__, lifespan=lifespan)
app.include_router(chat.router)
app.include_router(documents.router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check used by the frontend proxy and dev tooling."""
    return {"status": "ok", "version": __version__}
