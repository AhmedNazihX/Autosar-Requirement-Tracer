"""Where a project's derived artifacts live on disk.

One module owns the answer so the ingestion CLI, the API startup wiring and
the tests cannot disagree about it. Everything is resolved from the
*manifest's* location (``manifest.project_root``), never from the process
CWD, so ``uv run`` from ``backend/`` and from the repo root agree — the same
rule ``ingestion.fetcher.docs_dir`` and ``ingestion.code_fetcher.repo_dir``
already follow for the fetched inputs.

Everything under ``data/`` is derived and gitignored: the PDFs, the cloned
repository snapshot, the SQLite registry and the Chroma store. Deleting the
directory and re-running the ingestion CLI must reproduce all of it.
"""

from __future__ import annotations

from pathlib import Path

from core.manifest import ProjectManifest

#: The SQLite registry (requirements, code units, threads, caches).
DB_FILENAME = "reqtrace.db"

#: Chroma's persistent store. One collection per ``project_id`` inside it.
CHROMA_DIRNAME = "chroma"

#: The deterministic extraction evidence written on every ingestion run.
EXTRACTION_REPORT_FILENAME = "extraction_report.md"

#: The WP1 gate artifact: hand-verifiable requirements sampled from the corpus.
SPOT_CHECK_FILENAME = "spot_check.md"


class PathError(Exception):
    """Raised when a manifest cannot say where its ``data/`` directory is."""


def data_dir(manifest: ProjectManifest) -> Path:
    """The repo's ``data/`` directory for ``manifest``.

    Raises :class:`PathError` rather than silently falling back to the CWD:
    writing a 100 MB index into whatever directory a test happened to run in
    is worse than a clear failure.
    """
    root = manifest.project_root
    if root is None:
        raise PathError(
            "manifest has no project_root (was it constructed directly instead of via "
            "load_manifest?) — pass an explicit path instead of relying on data_dir()"
        )
    return root / "data"


def db_path(manifest: ProjectManifest) -> Path:
    """The SQLite database file for ``manifest``."""
    return data_dir(manifest) / DB_FILENAME


def chroma_dir(manifest: ProjectManifest) -> Path:
    """The Chroma persistent-store directory for ``manifest``."""
    return data_dir(manifest) / CHROMA_DIRNAME


def extraction_report_path(manifest: ProjectManifest) -> Path:
    """Where ``extraction_report.md`` is written."""
    return data_dir(manifest) / EXTRACTION_REPORT_FILENAME


def spot_check_path(manifest: ProjectManifest) -> Path:
    """Where ``spot_check.md`` — the WP1 gate artifact — is written."""
    return data_dir(manifest) / SPOT_CHECK_FILENAME
