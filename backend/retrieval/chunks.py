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

    Modules present only in the code (``CanNm``, ``PduR``, ``Com`` here, whose
    specifications are deliberately not ingested) keep their annotation-map
    spelling, which is the only spelling available for them.
    """
    return [
        *manifest.code.annotation_module_map.values(),
        *(document.module for document in manifest.documents),
    ]


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
