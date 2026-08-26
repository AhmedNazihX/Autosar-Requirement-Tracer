"""FastAPI application entrypoint for the ReqTrace backend."""

from __future__ import annotations

from fastapi import FastAPI

__version__ = "0.1.0"

app = FastAPI(title="ReqTrace API", version=__version__)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness check used by the frontend proxy and dev tooling."""
    return {"status": "ok", "version": __version__}
