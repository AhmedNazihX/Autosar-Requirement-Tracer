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
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from api import deps
from core import db
from core.models import Requirement
from ingestion.code_fetcher import repo_dir
from ingestion.fetcher import docs_dir
from retrieval.lookup import InvalidRequirementId, lookup

router = APIRouter(tags=["documents"])

#: Stands in for "no upper bound" when a caller asks which requirements a whole
#: file is tied to. Larger than any source file that could be indexed.
WHOLE_FILE = 10**9

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


class ImplementationLink(BaseModel):
    """One code unit tied to a requirement, and how the tie was made."""

    model_config = ConfigDict(extra="forbid")

    repo_path: str
    symbol: str
    kind: str
    line_span: tuple[int, int]
    git_sha: str
    #: ``annotation`` — a developer comment in this unit names the requirement.
    #: ``symbol`` — the requirement's own text names this function.
    found_by: Literal["annotation", "symbol"]
    #: Only for ``found_by="annotation"``: what that comment claimed. ``!req``
    #: is carried through rather than dropped, because a developer saying a
    #: requirement is *not* implemented here is a link worth showing.
    claim: str | None = None
    annotation_lines: list[int] = Field(default_factory=list)


class ImplementationResponse(BaseModel):
    """Where a requirement is implemented, as far as free evidence can say."""

    model_config = ConfigDict(extra="forbid")

    req_id: str
    git_sha: str
    links: list[ImplementationLink]
    #: A verdict **only if one is already cached**. This endpoint never judges.
    verdict: dict | None = None


@router.get("/requirements/{req_id}/implementation", response_model=ImplementationResponse)
def get_requirement_implementation(
    req_id: str, state: deps.AppState = Depends(deps.state_of)
) -> ImplementationResponse:
    """The code tied to a requirement — for free, and without judging it.

    This exists so the two halves of a citation can be opened together. Before
    it, clicking a requirement opened its page and left the code pane empty,
    and the user had to work out for themselves which part of the snapshot the
    requirement corresponded to — which is the one job this product exists to
    do for them.

    **It costs nothing and it never calls a model.** Both link kinds are plain
    SQL: tier-1 annotations (``@req``/``!req`` comments naming the requirement)
    and tier-2 anchors (the C symbols the requirement's own text names). A
    verdict is returned only when one is *already* in the cache — opening a
    citation must never start spending money, and a judged verdict is what
    ``check_implementation`` and the report are for.

    Ordering is by strength of claim: code the developers say implements this,
    then code the requirement names, then code the developers explicitly say
    does *not* implement it. The last group is still returned — it is a real
    link and hiding it would overstate coverage — but it never leads.
    """
    ready = _require_ready(state)
    try:
        hit = lookup(ready.pool, ready.manifest.project_id, req_id)
    except InvalidRequirementId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if hit is None:
        raise HTTPException(
            status_code=404, detail=f"No requirement {req_id!r} in this corpus."
        )

    requirement = hit.requirement
    canonical = Requirement.canonical_id(requirement.id)
    project_id = ready.manifest.project_id
    git_sha = ready.manifest.code.git_sha

    seen: set[tuple[str, str, str, tuple[int, int]]] = set()
    claimed: list[ImplementationLink] = []
    denied: list[ImplementationLink] = []
    anchored: list[ImplementationLink] = []

    for unit in db.list_code_units_by_annotation(ready.pool, project_id, canonical):
        matching = [
            annotation
            for annotation in unit.req_annotations
            if Requirement.canonical_id(annotation.canonical_id) == canonical
        ]
        # A unit that both claims and denies the same id is contradicting
        # itself; report the denial, which is the specific statement.
        claim = (
            "claimed_not_implemented"
            if any(a.claim == "claimed_not_implemented" for a in matching)
            else "claimed_implemented"
        )
        link = _link(unit, "annotation", claim, sorted(a.line for a in matching))
        seen.add(_identity(unit))
        (denied if claim == "claimed_not_implemented" else claimed).append(link)

    for symbol in requirement.named_symbols:
        for unit in db.list_code_units_by_symbol(ready.pool, project_id, symbol.strip()):
            if _identity(unit) in seen:
                continue
            seen.add(_identity(unit))
            anchored.append(_link(unit, "symbol", None, []))

    return ImplementationResponse(
        req_id=requirement.id,
        git_sha=git_sha,
        links=[
            *sorted(claimed, key=_by_strength),
            *sorted(anchored, key=_by_strength),
            *sorted(denied, key=_by_strength),
        ],
        verdict=db.get_verdict(
            ready.pool, canonical, git_sha, ready.manifest.models.judge
        ),
    )


def _link(unit, found_by: str, claim: str | None, lines: list[int]) -> ImplementationLink:
    return ImplementationLink(
        repo_path=unit.repo_path,
        symbol=unit.symbol,
        kind=unit.kind,
        line_span=unit.line_span,
        git_sha=unit.git_sha,
        found_by=found_by,
        claim=claim,
        annotation_lines=lines,
    )


def _identity(unit) -> tuple[str, str, str, tuple[int, int]]:
    """A code unit's primary key — symbols are not unique in a file (C5)."""
    return (unit.repo_path, unit.kind, unit.symbol, unit.line_span)


def _by_strength(link: ImplementationLink) -> tuple[int, str, int]:
    """Definitions before declarations: a prototype is not an implementation."""
    return (0 if link.kind == "function" else 1, link.repo_path, link.line_span[0])


@router.get("/documents/{doc}/file")
def get_document_file(
    doc: str, state: deps.AppState = Depends(deps.state_of)
) -> FileResponse:
    """The document's PDF bytes, for pdf.js to render (story S5.3.1).

    **Spec §6 does not list this endpoint and spec §7 cannot work without it.**
    §7 requires Tab A to render "the real SWS PDF page via pdf.js", and
    ``/documents/{doc}/view`` deliberately returns coordinates only — so
    nothing in the design actually hands the browser a PDF. This closes that
    gap and nothing more.

    **There is no path to traverse.** ``doc`` is a manifest *key*, matched
    against the manifest's own document list; the filename comes from the
    matched entry, never from the request. A caller cannot express a path here
    even in principle, which is a stronger position than sanitising one — the
    same rule ``/code/{path}`` has to enforce the hard way because its input
    really is a path.

    The PDFs are fetched to a gitignored ``data/`` directory at ingestion and
    served only to the local user who fetched them; this is a local-only
    application (spec §11) and nothing here republishes AUTOSAR's documents.
    """
    ready = _require_ready(state)
    entry = next((one for one in ready.manifest.documents if one.key == doc), None)
    if entry is None:
        known = ", ".join(one.key for one in ready.manifest.documents)
        raise HTTPException(
            status_code=404,
            detail=f"No document {doc!r} in this corpus. Known documents: {known}.",
        )

    path = docs_dir(ready.manifest) / entry.filename
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"{entry.filename} has not been fetched. Run the ingestion CLI — "
                "the PDFs live in a gitignored data/ directory, so a fresh clone "
                "has none of them."
            ),
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        # Inline: pdf.js reads it, the browser must not offer to save it.
        headers={"Content-Disposition": f'inline; filename="{entry.filename}"'},
    )


class LinkedRequirement(BaseModel):
    """A requirement a piece of code is tied to, ready to open in the pane."""

    model_config = ConfigDict(extra="forbid")

    req_id: str
    doc: str
    doc_title: str
    page: int
    page_count: int
    bbox: tuple[float, float, float, float] | None
    section: str | None
    quote: str
    #: ``annotation`` — a comment in this code names the requirement.
    #: ``symbol`` — the requirement's text names a symbol defined here.
    found_by: Literal["annotation", "symbol"]
    claim: str | None = None
    #: The unit the link came through, so the UI can say where.
    via_symbol: str
    via_kind: str


class CodeRequirementsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_path: str
    line_span: tuple[int, int]
    requirements: list[LinkedRequirement]


# Registered BEFORE the code-slice route below: `{path:path}` is greedy but
# backtracks, so a URL ending in `/requirements` matches here and anything else
# falls through to the slice. The only cost is that a source file literally
# named `requirements` would be unreachable, and none exists in a C snapshot.
@router.get("/code/{path:path}/requirements", response_model=CodeRequirementsResponse)
def get_code_requirements(
    path: str,
    lines: str | None = Query(default=None, max_length=32, pattern=r"^\d+-\d+$"),
    state: deps.AppState = Depends(deps.state_of),
) -> CodeRequirementsResponse:
    """Which requirements this code is tied to — the reverse of
    ``/requirements/{id}/implementation``.

    Same two free links, read the other way: the ``@req``/``!req`` comments in
    the code units covering this span, and the requirements whose own text
    names a symbol defined here. No model call, no cost, and no verdict — this
    answers "what does this code claim to implement", not "does it".

    One code unit routinely names several requirements — a file-header comment
    block can carry a dozen — so this returns all of them rather than pretending
    there is one answer.
    """
    ready = _require_ready(state)
    project_id = ready.manifest.project_id
    # Parsed here rather than through `_span`, which clamps against a file it
    # has read from disk. This endpoint answers from the index alone, so an
    # absent range means "the whole file".
    if lines:
        first_text, _, last_text = lines.partition("-")
        first, last = int(first_text), int(last_text)
        if last < first:
            raise HTTPException(
                status_code=400, detail=f"Lines {lines} run backwards."
            )
    else:
        first, last = 1, WHOLE_FILE

    units = db.list_code_units_in_span(ready.pool, project_id, path, first, last)
    if not units:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No indexed code at {path!r} lines {first}-{last}. The pane can "
                "only open paths inside the pinned snapshot."
            ),
        )

    titles = {entry.key: entry.title for entry in ready.manifest.documents}
    claimed: list[LinkedRequirement] = []
    denied: list[LinkedRequirement] = []
    anchored: list[LinkedRequirement] = []
    seen: set[str] = set()

    def add(
        requirement, found_by: str, claim: str | None, unit, bucket: list
    ) -> None:
        if requirement.id in seen:
            return
        seen.add(requirement.id)
        bucket.append(
            LinkedRequirement(
                req_id=requirement.id,
                doc=requirement.source_doc,
                doc_title=titles.get(requirement.source_doc, requirement.source_doc),
                page=requirement.page,
                page_count=ready.page_counts.get(requirement.source_doc, 0),
                bbox=requirement.bbox,
                section=requirement.section_path,
                quote=requirement.text,
                found_by=found_by,
                claim=claim,
                via_symbol=unit.symbol,
                via_kind=unit.kind,
            )
        )

    for unit in units:
        for annotation in unit.req_annotations:
            found = db.get_requirement(ready.pool, project_id, annotation.canonical_id)
            # An annotated id that resolves to nothing is release drift, not an
            # error — finding A4 measures it at about a third of them.
            if found is None:
                continue
            bucket = (
                denied if annotation.claim == "claimed_not_implemented" else claimed
            )
            add(found, "annotation", annotation.claim, unit, bucket)

    for unit in units:
        for requirement in db.list_requirements_naming_symbol(
            ready.pool, project_id, unit.symbol
        ):
            add(requirement, "symbol", None, unit, anchored)

    return CodeRequirementsResponse(
        repo_path=path,
        line_span=(first, last),
        requirements=[*claimed, *anchored, *denied],
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
