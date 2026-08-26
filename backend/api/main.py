"""FastAPI application entrypoint for the ReqTrace backend."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from core.config import get_settings
from retrieval.startup import load_indexes_from_settings

__version__ = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Build the in-memory BM25 index from SQLite before serving (spec §3).

    Never fatal. A fresh clone has no ``data/`` yet, so the index comes up
    empty with ``indexes.error`` explaining how to build it — the first-run
    setup screen (story S3.6.1) needs the server up in order to say so.
    """
    app.state.indexes = load_indexes_from_settings(get_settings().project_manifest)
    yield


app = FastAPI(title="ReqTrace API", version=__version__, lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check used by the frontend proxy and dev tooling."""
    return {"status": "ok", "version": __version__}
