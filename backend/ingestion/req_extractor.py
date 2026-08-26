"""Requirement extraction from a parsed SWS document (story S1.3.3).

Turns a :class:`~ingestion.pdf_loader.ParsedDocument` plus a manifest entry
into ``list[Requirement]``. Nothing here re-parses the PDF and nothing here
hard-codes a corpus pattern: every regex comes from the manifest's
``extraction`` section, which is the corpus-abstraction boundary the design
spec (§2) defines.

The block form this recognises is::

    [<ID>] <optional title> ⌈<requirement text>⌋(<optional upstream refs>)

Every part of :func:`build_block_pattern` is load-bearing, measured against
the real corpus:

===================================  ==========================
if this is dropped                   corpus recall falls to
===================================  ==========================
the cleaning pass                    ~73–76%
the ``title`` group                  ~73–76%
the ``_CONSTR`` id alternative       ~97% (≈20 requirements)
nothing                              1054/1054 across four docs
===================================  ==========================

The ``title`` group carries more weight than its name suggests. Because it
forbids ``[`` and ``]``, the regex engine cannot start a match at a *bracketed
reference* (``see [SWS_Can_00189]``) and run forward to some later
requirement's body: any intervening bracket blocks it. That single character
class is what makes the §6 "Requirements Tracing" table — pages of bracketed
ids with no ``⌈`` anywhere — yield zero requirements.

Locators: ``char_span`` is always set and always in *raw* document
coordinates (see :mod:`ingestion.mapped_text` for how cleaned-text matches
are translated back). ``bbox`` is set only when it can be justified — a
single block, or the union of blocks on one page. A requirement whose span
crosses a page break gets ``bbox=None``, because no rectangle on either page
is the truth; the viewer falls back to page-without-highlight.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from core.manifest import DocumentEntry, ProjectManifest
from core.models import Requirement
from ingestion.mapped_text import MappedText, clean_with_offsets
from ingestion.pdf_loader import ParsedDocument, TextBlock
from ingestion.text_norm import extract_named_symbols, normalize_requirement_text

#: Upper bound on the optional title between ``]`` and the body opener. Long
#: enough for every titled requirement in the corpus ("{DRAFT} Definition of
#: API function Can_EnableEgressTimeStamp" is 56 characters), short enough
#: that a runaway match cannot swallow a paragraph of prose.
TITLE_MAX_CHARS = 200

#: A section heading, as PyMuPDF renders it: a block holding exactly two
#: lines, the section number and the section title (``"7.6\nL-PDU
#: reception\n"``). The block boundary is what makes this reliable — the
#: number and the title are never merged with body text, and the multi-line
#: document-history rows that also start with a version-like number never
#: match because they carry more than two lines.
HEADING_BLOCK_PATTERN = re.compile(
    r"\A(?P<number>(?:\d+|[A-Z])(?:\.\d+)*)\n(?P<title>[^\n]{1,160})\n\Z"
)

#: How much cleaned text either side of an unmatched body opener goes into the
#: miss report, so a human can judge it without opening the PDF.
MISS_CONTEXT_CHARS = 260

#: A bracketed token immediately before an unmatched body opener, reported as
#: a diagnostic: it is almost always the id that failed ``req_id_pattern``.
BRACKETED_TOKEN_PATTERN = re.compile(r"\[([^\[\]\n]{1,60})\]")


class ExtractionError(Exception):
    """Raised when extraction cannot proceed (e.g. an unusable manifest)."""


@dataclass(frozen=True)
class Heading:
    """One detected section heading, located in raw document coordinates."""

    number: str
    title: str
    source_offset: int
    page: int

    @property
    def path(self) -> str:
        """``"7.6 L-PDU reception"`` — what ``Requirement.section_path`` gets."""
        return f"{self.number} {self.title}"


@dataclass(frozen=True)
class RequirementMiss:
    """A body opener in the cleaned text that produced no requirement.

    Recorded rather than swallowed: the miss list is the evidence that the
    deterministic extractor is not quietly dropping requirements, and it is
    what ``data/extraction_report.md`` exists to show.
    """

    page: int
    source_offset: int
    nearest_bracketed: str | None
    context: str


@dataclass(frozen=True)
class DocumentExtraction:
    """Everything :func:`extract_requirements` learned about one document."""

    document_key: str
    #: The document's identity as every downstream reader sees it: the
    #: **manifest key**, matching ``Requirement.source_doc``. The PDF filename
    #: lives on the manifest entry (``entry.filename``), which is where the
    #: fetcher and the human-facing artifacts read it from.
    source_doc: str
    module: str
    title: str
    page_count: int
    expected_requirements: int
    requirements: list[Requirement]
    misses: list[RequirementMiss]
    headings: list[Heading]
    #: Cleaned text with its origin index — reused by the context chunker
    #: rather than cleaning the document a second time.
    cleaned: MappedText
    #: ``⌈…⌋`` spans in *cleaned* coordinates, matched or not. The context
    #: chunker subtracts these so no character is counted twice.
    body_spans: list[tuple[int, int]]
    #: Full requirement-block spans (``[id] title ⌈body⌋(refs)``) in cleaned
    #: coordinates, one per extracted requirement.
    requirement_spans: list[tuple[int, int]]
    warnings: list[str] = field(default_factory=list)

    @property
    def body_opener_count(self) -> int:
        return len(self.body_spans)

    @property
    def indexed_spans(self) -> list[tuple[int, int]]:
        """Cleaned spans already accounted for by a requirement chunk.

        The union of :attr:`body_spans` and :attr:`requirement_spans`, merged
        and sorted. Context chunking subtracts this: a body is excluded
        because indexing it twice would put the same sentence in both pools,
        and the surrounding ``[id]``/``(refs)`` scaffolding is excluded
        because it is metadata on the requirement record, not prose.
        """
        return merge_spans([*self.body_spans, *self.requirement_spans])


def build_block_pattern(manifest: ProjectManifest) -> re.Pattern[str]:
    """Compile the requirement-block regex from the manifest's patterns.

    Uses ``extraction.req_id_pattern`` (corpus-wide) rather than a document's
    own ``req_id_pattern``. The two differ in practice: the CAN Driver
    document spells seven of its own requirement ids ``SWS_CAN_00496`` where
    the rest of the document writes ``SWS_Can_00496``, and the per-document
    pattern is case-sensitive, so it silently drops those seven. The
    corpus-wide pattern accepts any module token and lands on the manifest's
    ``expected_requirements`` exactly for all four documents.
    """
    extraction = manifest.extraction
    opener = re.escape(extraction.body_open)
    closer = re.escape(extraction.body_close)
    try:
        return re.compile(
            rf"\[(?P<id>{extraction.req_id_pattern})\]"
            rf"(?P<title>[^{opener}{closer}\[\]]{{0,{TITLE_MAX_CHARS}}}?)"
            rf"{opener}(?P<text>.*?){closer}"
            rf"\s*(?:\((?P<refs>[^)]*)\))?",
            re.S,
        )
    except re.error as exc:  # pragma: no cover - manifest validation catches this first
        raise ExtractionError(f"cannot build the requirement-block pattern: {exc}") from exc


def build_body_pattern(manifest: ProjectManifest) -> re.Pattern[str]:
    """Compile a bare ``⌈…⌋`` scanner — every body, requirement or not."""
    extraction = manifest.extraction
    return re.compile(
        rf"{re.escape(extraction.body_open)}.*?{re.escape(extraction.body_close)}", re.S
    )


def clean_document(document: ParsedDocument, manifest: ProjectManifest, entry: DocumentEntry):
    """Clean ``document.text`` while keeping a map back to raw offsets."""
    return clean_with_offsets(
        document.text,
        footer_patterns=manifest.extraction.footer_patterns,
        drop_markers=manifest.extraction.drop_markers,
        document_title=entry.title,
    )


def find_headings(document: ParsedDocument) -> list[Heading]:
    """Every section heading in the document, in reading order.

    Detected on the *raw* blocks: page furniture never looks like a heading,
    and the block boundary is the signal (see :data:`HEADING_BLOCK_PATTERN`).
    """
    headings: list[Heading] = []
    for page_number, block in document.iter_blocks():
        match = HEADING_BLOCK_PATTERN.match(block.text)
        if match is None:
            continue
        headings.append(
            Heading(
                number=match.group("number"),
                title=match.group("title").strip(),
                source_offset=block.char_start,
                page=page_number,
            )
        )
    return headings


def heading_for_offset(headings: Sequence[Heading], source_offset: int) -> Heading | None:
    """The innermost heading in force at ``source_offset``, if any."""
    offsets = [heading.source_offset for heading in headings]
    index = bisect_right(offsets, source_offset) - 1
    return headings[index] if index >= 0 else None


def blocks_overlapping(
    document: ParsedDocument, page_number: int, start: int, end: int
) -> list[TextBlock]:
    """Blocks on ``page_number`` whose raw span overlaps ``[start, end)``."""
    return [
        block
        for block in document.page(page_number).blocks
        if block.char_start < end and block.char_end > start
    ]


def locate_span(
    document: ParsedDocument, start: int, end: int
) -> tuple[int, tuple[float, float, float, float] | None]:
    """Resolve a raw span to ``(page, bbox)``.

    ``page`` is always where the span *starts*. ``bbox`` is the block
    rectangle when the span sits in one block, the union when it covers
    several blocks on one page, and ``None`` when the span crosses a page
    break — no rectangle would be honest there.
    """
    pages = document.pages_for_span(start, end)
    page_number = pages[0]
    if len(pages) > 1:
        return page_number, None

    blocks = blocks_overlapping(document, page_number, start, end)
    if not blocks:
        return page_number, None
    return page_number, (
        min(block.bbox[0] for block in blocks),
        min(block.bbox[1] for block in blocks),
        max(block.bbox[2] for block in blocks),
        max(block.bbox[3] for block in blocks),
    )


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort ``spans`` and merge every overlapping or touching pair."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


def _nearest_bracketed(cleaned_text: str, offset: int) -> str | None:
    """The last bracketed token before ``offset``, for the miss report."""
    window = cleaned_text[max(0, offset - MISS_CONTEXT_CHARS) : offset]
    matches = BRACKETED_TOKEN_PATTERN.findall(window)
    return matches[-1].strip() if matches else None


def extract_requirements(
    document: ParsedDocument, manifest: ProjectManifest, entry: DocumentEntry
) -> DocumentExtraction:
    """Extract every requirement in ``document``, plus the misses.

    Never raises on a count mismatch against ``entry.expected_requirements``:
    a corpus republished with three more requirements must not break
    ingestion. The deviation is recorded in
    :attr:`DocumentExtraction.warnings` and surfaces in the extraction report.
    """
    cleaned = clean_document(document, manifest, entry)
    headings = find_headings(document)
    block_pattern = build_block_pattern(manifest)
    upstream_pattern = re.compile(manifest.extraction.upstream_id_pattern)
    symbol_pattern = manifest.extraction.symbol_pattern

    def normalize(text: str) -> str:
        return normalize_requirement_text(
            text,
            footer_patterns=manifest.extraction.footer_patterns,
            drop_markers=manifest.extraction.drop_markers,
            document_title=entry.title,
        )

    requirements: list[Requirement] = []
    warnings: list[str] = []
    matched_spans: list[tuple[int, int]] = []

    for match in block_pattern.finditer(cleaned.text):
        text = normalize(match.group("text"))
        if not text:
            # A ⌈⌋ with nothing between it is not a requirement; fall through
            # to the miss list so it is reported rather than dropped.
            continue

        source_start, source_end = cleaned.source_span(*match.span())
        page, bbox = locate_span(document, source_start, source_end)
        title = normalize(match.group("title") or "") or None
        refs = normalize(match.group("refs") or "")
        heading = heading_for_offset(headings, source_start)

        requirements.append(
            Requirement(
                id=match.group("id"),
                title=title,
                text=text,
                section_path=heading.path if heading else None,
                page=page,
                bbox=bbox,
                char_span=(source_start, source_end),
                source_doc=entry.key,
                upstream_ids=_dedupe(m.group(0) for m in upstream_pattern.finditer(refs)),
                named_symbols=extract_named_symbols(text, symbol_pattern),
                version=manifest.version,
                project_id=manifest.project_id,
                doc_type="requirement",
            )
        )
        matched_spans.append(match.span())

    body_spans = [m.span() for m in build_body_pattern(manifest).finditer(cleaned.text)]
    misses = _collect_misses(document, cleaned, body_spans, matched_spans)

    counts = Counter(req.id for req in requirements)
    duplicates = sorted(req_id for req_id, count in counts.items() if count > 1)
    if duplicates:
        warnings.append(f"duplicate requirement ids extracted: {', '.join(duplicates)}")
    if len(requirements) != entry.expected_requirements:
        warnings.append(
            f"extracted {len(requirements)} requirements but the manifest expects "
            f"{entry.expected_requirements} (delta "
            f"{len(requirements) - entry.expected_requirements:+d})"
        )

    return DocumentExtraction(
        document_key=entry.key,
        source_doc=entry.key,
        module=entry.module,
        title=entry.title,
        page_count=document.page_count,
        expected_requirements=entry.expected_requirements,
        requirements=requirements,
        misses=misses,
        headings=headings,
        cleaned=cleaned,
        body_spans=body_spans,
        requirement_spans=list(matched_spans),
        warnings=warnings,
    )


def _collect_misses(
    document: ParsedDocument,
    cleaned: MappedText,
    body_spans: Sequence[tuple[int, int]],
    matched_spans: Sequence[tuple[int, int]],
) -> list[RequirementMiss]:
    """Every body span that no requirement match covers."""
    misses: list[RequirementMiss] = []
    for start, end in body_spans:
        if any(m_start <= start and end <= m_end for m_start, m_end in matched_spans):
            continue
        source_offset = cleaned.source_offset(start)
        window_start = max(0, start - MISS_CONTEXT_CHARS)
        window_end = min(len(cleaned.text), end + MISS_CONTEXT_CHARS)
        misses.append(
            RequirementMiss(
                page=document.page_for_char(source_offset),
                source_offset=source_offset,
                nearest_bracketed=_nearest_bracketed(cleaned.text, start),
                context=re.sub(r"\s+", " ", cleaned.text[window_start:window_end]).strip(),
            )
        )
    return misses
