"""What an indexed chunk *is*: its stable id, its text and its metadata.

Both indexes are built from this one module so they cannot disagree. That
matters for more than tidiness: the retrieval pipeline (spec §4) fuses BM25
and dense hits with Reciprocal Rank Fusion, which can only recognise that two
hits are the same chunk if both indexes name it identically. So the id functions
live here, and :mod:`retrieval.vector_store` and :mod:`retrieval.bm25` both
import them.

Ids are **deterministic functions of identity, never of order** — a
requirement's own id, or a code unit's full storage key. Re-running ingestion
therefore upserts over the same rows instead of appending duplicates, which is
what makes the pipeline idempotent.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

from core.manifest import ProjectManifest
from core.models import CodeUnit, Requirement

#: ``doc_type`` for a code chunk. ``Requirement.doc_type`` covers
#: ``requirement`` and ``context``; code units are the third kind of thing in
#: the collection, and — per the design's metadata plan — ``doc_type`` plus the
#: presence of ``repo_path`` is what separates the three.
CODE_DOC_TYPE = "code"

#: Metadata values Chroma accepts. It rejects ``None`` and sequences, so lists
#: are joined and empty values are dropped rather than stored as ``""``.
MetadataValue = str | int | float | bool


@dataclass(frozen=True)
class IndexDocument:
    """One chunk as both indexes see it: stable id, text to match, metadata."""

    id: str
    text: str
    metadata: dict[str, MetadataValue]


def requirement_chunk_key(req_id: str) -> str:
    """Stable chunk id for a requirement or context chunk id.

    ``Requirement.id`` is already unique within a project (it is half the
    ``requirements`` primary key) and context chunks carry D4's synthetic
    ``CTX_<doc>_<section>_<nn>`` ids, which cannot collide with a ``SWS_*``
    id. The collection is per project, so the project needs no prefix.

    Takes the raw id rather than the model so the BM25 loader — which reads
    narrow SQLite rows, not ``Requirement`` objects — can produce byte-identical
    ids without a second copy of this format string.
    """
    return f"req:{req_id}"


def code_chunk_key(repo_path: str, kind: str, symbol: str, line_span: tuple[int, int]) -> str:
    """Stable chunk id for a code unit — its full storage key, in one string.

    Mirrors the ``code_units`` primary key exactly (``repo_path``, ``kind``,
    ``symbol``, ``line_span``). ``symbol`` alone is not unique within a file
    and neither is ``(repo_path, symbol)``, so a shorter key would silently
    collapse a struct onto the typedef that names it.
    """
    return f"code:{repo_path}:{kind}:{symbol}:{line_span[0]}-{line_span[1]}"


def requirement_chunk_id(requirement: Requirement) -> str:
    """:func:`requirement_chunk_key` for a :class:`Requirement`."""
    return requirement_chunk_key(requirement.id)


def code_chunk_id(unit: CodeUnit) -> str:
    """:func:`code_chunk_key` for a :class:`CodeUnit`."""
    return code_chunk_key(unit.repo_path, unit.kind, unit.symbol, unit.line_span)


def module_names(manifest: ProjectManifest) -> list[str]:
    """Module names for :func:`module_for_path`, documents' spelling winning.

    Two places in the manifest name modules and they disagree on casing.
    ``code.annotation_module_map``'s *values* are chosen to build canonical
    requirement ids (``SWS_CANIF_00023``), so CanIf appears there as
    ``CANIF``; ``documents[*].module`` is the spelling requirement chunks are
    tagged with (``CanIf``). Later entries win in :func:`module_for_path`, so
    the document spelling is appended last — otherwise a code chunk and a
    requirement chunk from the same module would carry two different ``module``
    values and no metadata filter could match both.

    A module present only in the code would keep its annotation-map spelling,
    the only one available for it. Since the CanNm, Com and PduR
    specifications joined the corpus no such module exists here, but the
    ordering rule still guards the case for the next manifest.
    """
    return [
        *manifest.code.annotation_module_map.values(),
        *(document.module for document in manifest.documents),
    ]


@dataclass(frozen=True)
class ChunkRef:
    """A chunk id taken apart into the storage key it was built from.

    Fusion (:mod:`retrieval.hybrid`) works on ids alone, so the pipeline needs
    a way back from an id to the ``Requirement`` or ``CodeUnit`` it names.
    Parsing lives here beside :func:`requirement_chunk_key` and
    :func:`code_chunk_key` so the format has exactly one definition — a parser
    that drifted from the builder would resolve some ids to the wrong row and
    others to none, both silently.
    """

    kind: str
    req_id: str | None = None
    repo_path: str | None = None
    code_kind: str | None = None
    symbol: str | None = None
    line_span: tuple[int, int] | None = None


def parse_chunk_key(chunk_id: str) -> ChunkRef | None:
    """Take ``chunk_id`` apart, or ``None`` if it is not one of ours.

    ``None`` rather than an exception: an unparseable id means the index holds
    something this build did not write, which the pipeline should skip rather
    than die on.

    Code keys split safely on ``:`` because none of their four parts can
    contain one — a POSIX repo path, a ``CodeUnitKind`` member, a C identifier
    and a line span. The split is bounded to four so a path containing an
    unexpected colon fails to parse instead of shifting every field left.
    """
    if chunk_id.startswith("req:"):
        req_id = chunk_id[4:]
        return ChunkRef(kind="requirement", req_id=req_id) if req_id else None

    if not chunk_id.startswith("code:"):
        return None

    parts = chunk_id[5:].split(":")
    if len(parts) != 4:
        return None
    repo_path, code_kind, symbol, span = parts
    start, _, end = span.partition("-")
    if not (repo_path and code_kind and symbol and start.isdigit() and end.isdigit()):
        return None
    return ChunkRef(
        kind="code",
        repo_path=repo_path,
        code_kind=code_kind,
        symbol=symbol,
        line_span=(int(start), int(end)),
    )


def module_by_document(manifest: ProjectManifest) -> dict[str, str]:
    """``source_doc`` → module, from the manifest's own document entries.

    A requirement's module is not a column in SQLite: it is a property of the
    document it came from, and ``documents[*].module`` is where that lives.
    Both indexes derive it through this one function so a requirement chunk and
    a code chunk from the same module cannot end up tagged differently — which
    would make a metadata filter (story S2.4.1) match one and not the other.
    """
    return {document.key: document.module for document in manifest.documents}


def module_for_path(repo_path: str, modules: Iterable[str]) -> str | None:
    """The module a repo path belongs to, using names taken from the manifest.

    ``modules`` is the manifest's ``code.annotation_module_map`` values, so
    no module name is hard-coded here. A path segment that matches one of
    them case-insensitively wins (``communication/CanIf/CanIf.c`` → ``CanIf``);
    ``None`` when nothing matches, in which case the key is simply omitted
    rather than stored as an empty string.
    """
    known = {name.upper(): name for name in modules}
    for segment in PurePosixPath(repo_path).parts[:-1]:
        match = known.get(segment.upper())
        if match is not None:
            return match
    return None


def _clean(metadata: Mapping[str, MetadataValue | None]) -> dict[str, MetadataValue]:
    """Drop keys with no value. Chroma rejects ``None``, and ``""`` filters badly."""
    return {
        key: value
        for key, value in metadata.items()
        if value is not None and not (isinstance(value, str) and not value)
    }


def requirement_document(requirement: Requirement, *, module: str | None = None) -> IndexDocument:
    """A requirement or context chunk as an :class:`IndexDocument`.

    The embedded text includes the title when there is one: a requirement's
    title is the most retrievable sentence it has (``Can_Write shall
    return...``), and leaving it out of the vector while keeping it in the
    metadata would make titled requirements *harder* to find than untitled
    ones.
    """
    text = f"{requirement.title}\n{requirement.text}" if requirement.title else requirement.text
    return IndexDocument(
        id=requirement_chunk_id(requirement),
        text=text,
        metadata=_clean(
            {
                "req_id": requirement.id,
                "canonical_id": Requirement.canonical_id(requirement.id),
                "doc_type": requirement.doc_type,
                "source_doc": requirement.source_doc,
                "module": module,
                "section_path": requirement.section_path,
                "page": requirement.page,
                "title": requirement.title,
                "version": requirement.version,
                "upstream_ids": ",".join(requirement.upstream_ids),
                "named_symbols": ",".join(requirement.named_symbols),
            }
        ),
    )


def code_document(unit: CodeUnit, *, module: str | None = None) -> IndexDocument:
    """A code unit as an :class:`IndexDocument`.

    ``unit.text`` already carries the ``// CanIf.c > CanIf_Transmit``
    breadcrumb the spec (§4) requires, so it is embedded verbatim.

    ``line_span`` is stored three ways on purpose: ``line_span`` as
    ``"120-168"`` is what a citation renders, while ``line_start`` and
    ``line_end`` are integers a metadata filter can actually compare against.
    """
    start, end = unit.line_span
    return IndexDocument(
        id=code_chunk_id(unit),
        text=unit.text,
        metadata=_clean(
            {
                "doc_type": CODE_DOC_TYPE,
                "repo_path": unit.repo_path,
                "symbol": unit.symbol,
                "kind": unit.kind,
                "language": unit.language,
                "module": module,
                "line_span": f"{start}-{end}",
                "line_start": start,
                "line_end": end,
                "git_sha": unit.git_sha,
                "claimed_ids": ",".join(unit.claimed_implemented_ids),
                "annotations": len(unit.req_annotations),
            }
        ),
    )
