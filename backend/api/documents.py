"""The read endpoints behind the source pane (story S3.3.2).

Three lookups, all of them reads of already-indexed data:

* ``GET /requirements/{req_id}`` — the requirement plus its citation payload,
  wrapping the F2.1 engine so the HTTP layer has no lookup logic of its own.
* ``GET /documents/{doc}/view?highlight=<req_id>`` — the page and bbox for the
  pdf.js overlay. It returns **coordinates, not an image**: the frontend renders
  the real PDF and draws the rectangle itself (spec §7).
* ``GET /code/{path}?lines=a-b`` — a slice of a file from the pinned snapshot.

**Path handling is the security-sensitive part** (spec §8). A repo path arrives
from the URL, so it is: resolved against the snapshot root, required to stay
inside it after resolution (which is what actually stops ``../``, since
string-matching ``..`` misses encodings and symlinks), and required to be a
file the *index* knows about. That last check is the strongest one — the answer
to "may I read this?" is "only if ingestion indexed it", so an unindexed file
inside the snapshot is refused too, and no filesystem walk can be steered by a
crafted path. Story S6.3.1 hardens the edges further; the invariants live here
because that is where the resolution happens.

A miss is a clean 404 with a sentence, never a stack trace.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from api import deps
from ingestion.code_fetcher import repo_dir
from retrieval.lookup import InvalidRequirementId, lookup

router = APIRouter(tags=["documents"])

#: Most lines one request may slice out of a file. A code pane shows a
#: function, not a translation unit, and an unbounded slice is a cheap way to
#: make the server read a megabyte per request.
MAX_LINES = 2000

#: Longest repo path accepted, before resolution (story S6.3.1's length cap).
MAX_PATH_CHARS = 512


class RequirementResponse(BaseModel):
    """A requirement and the citation payload that points at it."""

    model_config = ConfigDict(extra="forbid")

    req_id: str
    title: str | None
    text: str
    section_path: str | None
    page: int
    doc: str
    doc_title: str
    page_count: int
    bbox: tuple[float, float, float, float] | None
    upstream_ids: list[str]
    named_symbols: list[str]
    version: str


class DocumentViewResponse(BaseModel):
    """Where to look in a document, for the pdf.js overlay.

    Coordinates only — the frontend renders the PDF itself. ``bbox`` is
    ``None`` for a requirement whose text crosses a page break.
    """

    model_config = ConfigDict(extra="forbid")

    doc: str
    doc_title: str
    filename: str
    page_count: int
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    highlight: str | None = None


class CodeSliceResponse(BaseModel):
    """A slice of a file from the pinned snapshot."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str
    git_sha: str
    language: str
    first_line: int
    last_line: int
    total_lines: int
    text: str


def _require_ready(state: deps.AppState) -> deps.AppState:
    if not state.ready or state.manifest is None or state.pool is None:
        raise HTTPException(
            status_code=503,
            detail=(
                state.error
                or "The corpus is not indexed yet. Run the ingestion CLI and restart."
            ),
        )
    return state


@router.get("/requirements/{req_id}", response_model=RequirementResponse)
def get_requirement(
    req_id: str,
    state: deps.AppState = Depends(deps.state_of),
) -> RequirementResponse:
    """One requirement by id, however loosely it is spelled (story S2.1.1)."""
    ready = _require_ready(state)
    try:
        hit = lookup(ready.pool, ready.manifest.project_id, req_id)
    except InvalidRequirementId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if hit is None:
        raise HTTPException(
            status_code=404,
            detail=f"No requirement {req_id!r} in this corpus.",
        )

    requirement = hit.requirement
    titles = {entry.key: entry.title for entry in ready.manifest.documents}
    return RequirementResponse(
        req_id=requirement.id,
        title=requirement.title,
        text=requirement.text,
        section_path=requirement.section_path,
        page=requirement.page,
        doc=requirement.source_doc,
        doc_title=titles.get(requirement.source_doc, requirement.source_doc),
        page_count=ready.page_counts.get(requirement.source_doc, 0),
        bbox=requirement.bbox,
        upstream_ids=list(requirement.upstream_ids),
        named_symbols=list(requirement.named_symbols),
        version=requirement.version,
    )


@router.get("/documents/{doc}/view", response_model=DocumentViewResponse)
def view_document(
    doc: str,
    highlight: str | None = Query(default=None, max_length=64),
    state: deps.AppState = Depends(deps.state_of),
) -> DocumentViewResponse:
    """Page and bbox for ``highlight`` in ``doc`` — coordinates, not an image."""
    ready = _require_ready(state)
    entry = next((one for one in ready.manifest.documents if one.key == doc), None)
    if entry is None:
        known = ", ".join(one.key for one in ready.manifest.documents)
        raise HTTPException(
            status_code=404,
            detail=f"No document {doc!r} in this corpus. Known documents: {known}.",
        )

    response = DocumentViewResponse(
        doc=entry.key,
        doc_title=entry.title,
        filename=entry.filename,
        page_count=ready.page_counts.get(entry.key, 0),
    )
    if highlight is None:
        return response

    try:
        hit = lookup(ready.pool, ready.manifest.project_id, highlight)
    except InvalidRequirementId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if hit is None:
        raise HTTPException(
            status_code=404, detail=f"No requirement {highlight!r} in this corpus."
        )
    if hit.requirement.source_doc != entry.key:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{hit.requirement.id} is in {hit.requirement.source_doc!r}, "
                f"not in {entry.key!r}."
            ),
        )

    return response.model_copy(
        update={
            "page": hit.requirement.page,
            "bbox": hit.requirement.bbox,
            "highlight": hit.requirement.id,
        }
    )


@router.get("/code/{path:path}", response_model=CodeSliceResponse)
def get_code(
    path: str,
    lines: str | None = Query(default=None, max_length=32, pattern=r"^\d+-\d+$"),
    state: deps.AppState = Depends(deps.state_of),
) -> CodeSliceResponse:
    """A slice of an indexed file from the pinned snapshot."""
    ready = _require_ready(state)
    if len(path) > MAX_PATH_CHARS:
        raise HTTPException(status_code=400, detail="Path is too long.")

    resolved, repo_path = _resolve_indexed(ready, path)
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{repo_path} is indexed but not readable in the snapshot at "
                f"{repo_dir(ready.manifest)} — re-run ingestion to restore it."
            ),
        ) from exc

    all_lines = content.splitlines()
    first, last = _span(lines, len(all_lines))
    return CodeSliceResponse(
        repo_path=repo_path,
        git_sha=ready.manifest.code.git_sha,
        language=ready.manifest.code.language,
        first_line=first,
        last_line=last,
        total_lines=len(all_lines),
        text="\n".join(all_lines[first - 1 : last]),
    )


def _resolve_indexed(state: deps.AppState, path: str) -> tuple[Path, str]:
    """Resolve ``path`` inside the snapshot, and only if the index knows it.

    Three checks, in increasing strength:

    1. it resolves to somewhere inside the snapshot root (this, not a ``..``
       substring test, is what defeats traversal — encodings and symlinks make
       string matching unreliable);
    2. the index has code units for it, so the answer to "may I read this?" is
       "only if ingestion indexed it";
    3. it is a regular file.

    Check 2 is the one that matters: it means no crafted path can make the
    server enumerate the filesystem, because the set of readable paths is a
    fixed list in SQLite rather than whatever is on disk.
    """
    root = repo_dir(state.manifest).resolve()
    repo_path = path.strip("/")
    candidate = (root / repo_path).resolve()

    if not candidate.is_relative_to(root):
        # Deliberately the same 404 as an unknown file: a distinct error here
        # would confirm to a prober that the path escaped.
        raise HTTPException(status_code=404, detail=f"No indexed file {path!r}.")

    known = state.pool.execute(
        "SELECT 1 FROM code_units WHERE project_id = ? AND repo_path = ? LIMIT 1",
        (state.manifest.project_id, repo_path),
    ).fetchone()
    if known is None:
        raise HTTPException(status_code=404, detail=f"No indexed file {path!r}.")
    if not candidate.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"{repo_path} is indexed but missing from the snapshot — "
                "re-run ingestion to restore it."
            ),
        )
    return candidate, repo_path


def _span(lines: str | None, total: int) -> tuple[int, int]:
    """The 1-based inclusive line range to return, clamped to the file."""
    if total == 0:
        return 1, 0
    if lines is None:
        return 1, min(total, MAX_LINES)
    first_text, _, last_text = lines.partition("-")
    first = max(int(first_text), 1)
    last = min(int(last_text), total)
    if first > total:
        raise HTTPException(
            status_code=416,
            detail=f"Lines {lines} are outside the file, which has {total} line(s).",
        )
    last = max(last, first)
    return first, min(last, first + MAX_LINES - 1)
