"""Context-prose chunking (story S1.3.5).

Everything in a cleaned SWS document that is **not** inside a ``⌈…⌋``
requirement body is candidate context prose: section introductions,
definitions, explanatory paragraphs, implementation hints. Those are emitted
as :class:`~core.models.Requirement` records with ``doc_type="context"``.

The distinction is load-bearing rather than cosmetic. ``doc_type`` is one of
the three self-query filter fields (module, section, doc_type) in the
retrieval pipeline (spec §4, story S2.4.1): requirement chunks must dominate
answers about *what is required*, context chunks answer *what something
means*. Mixing the two into one pool makes both worse.

Paragraph boundaries come from PyMuPDF's blocks, not from blank-line
heuristics: a block *is* a paragraph in this corpus, and its char span is
already known. Chunks therefore never split mid-sentence at a paragraph
boundary, and only merge blocks that are genuinely adjacent — a requirement
body, a heading or a figure caption between two blocks flushes the chunk
rather than being silently bridged.

What is deliberately dropped, and why (all counted in
:class:`ChunkingStats`, which the extraction report prints):

* **front matter** — everything before the document's first numbered heading:
  the title page, the legal disclaimer, the document change-history table,
  the table of contents and the lists of figures and tables. One structural
  rule instead of four page-range hacks.
* **requirement bodies** — every ``⌈…⌋`` span, whether or not it produced a
  requirement, so no character is indexed twice.
* **headings** — the heading text becomes chunk metadata (``title``,
  ``section_path``), so re-indexing it as prose would be duplication.
* **figure and table captions** — ``Figure 7.4: …``, ``Table 3: …``. Pure
  page furniture for retrieval purposes.
* **bibliography and change-history sections** — see
  :data:`EXCLUDED_SECTION_RULES`.
* **short residue** — anything below :data:`MIN_CHUNK_CHARS` after all of the
  above. This is the rule that keeps stray table cells, page numbers and the
  ``()`` left behind next to a requirement's reference list out of the index,
  where they would otherwise hurt BM25 precision.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from core.manifest import DocumentEntry, ProjectManifest
from core.models import Requirement
from ingestion.mapped_text import MappedText
from ingestion.pdf_loader import ParsedDocument
from ingestion.req_extractor import (
    HEADING_BLOCK_PATTERN,
    DocumentExtraction,
    Heading,
    heading_for_offset,
    locate_span,
)
from ingestion.text_norm import extract_named_symbols, normalize_requirement_text

#: Below this many characters a chunk is page-furniture residue, not prose.
MIN_CHUNK_CHARS = 120

#: Chunks are flushed once they reach this size, so the usual chunk lands in
#: the 400–800 band the design asks for.
TARGET_CHUNK_CHARS = 400

#: Hard ceiling. Guaranteed, not aspirational: a paragraph longer than this is
#: pre-split at a sentence boundary (or, failing that, at a word boundary)
#: before packing, and packing never crosses the ceiling.
MAX_CHUNK_CHARS = 800

#: Figure and table captions. ``Figure 10.8: CanTpNTa … overview``.
CAPTION_PATTERN = re.compile(r"\A(?:Figure|Table)\s+[A-Za-z0-9.\-]+\s*:", re.I)

#: Sentence-ish boundaries used when a single paragraph exceeds the ceiling.
SENTENCE_BOUNDARY_PATTERN = re.compile(r"(?<=[.;:!?])\s+")

#: Sections excluded wholesale, by heading title, together with every
#: descendant heading (``3 Related documentation`` takes ``3.1`` and ``3.2``
#: with it; ``B Change History`` takes ``B.1.1``…``B.1.6``). Keyed by the name
#: the extraction report attributes the removed chunks to.
EXCLUDED_SECTION_RULES: dict[str, re.Pattern[str]] = {
    "bibliography": re.compile(r"\Arelated\s+document", re.I),
    "change_history": re.compile(r"\Achange\s+history", re.I),
}

#: Stand-in section number for a chunk with no heading in force. Should not
#: occur once front matter is dropped, but chunk ids must stay well-formed.
UNSECTIONED = "NA"


@dataclass
class ChunkingStats:
    """Why prose did not become a chunk. Every field is reported."""

    blocks_total: int = 0
    blocks_front_matter: int = 0
    blocks_headings: int = 0
    blocks_excluded_sections: dict[str, int] = field(default_factory=dict)
    blocks_captions: int = 0
    #: Blocks that a requirement span cut into, wholly or partly.
    blocks_touching_requirements: int = 0
    residues_considered: int = 0
    chunks_emitted: int = 0
    chunks_dropped_short: int = 0

    @property
    def blocks_excluded_sections_total(self) -> int:
        return sum(self.blocks_excluded_sections.values())


@dataclass(frozen=True)
class _Unit:
    """One prose fragment, in cleaned coordinates, with its final text."""

    cleaned_start: int
    cleaned_end: int
    text: str


def _excluded_section_rule(heading: Heading) -> str | None:
    for name, pattern in EXCLUDED_SECTION_RULES.items():
        if pattern.search(heading.title):
            return name
    return None


def excluded_section_roots(headings: Sequence[Heading]) -> dict[str, str]:
    """Map each excluded heading *number* to the rule that excluded it.

    Only roots are returned; :func:`is_excluded_section` handles descendants
    by number prefix, so a subsection that does not itself match the rule
    (``3.1 Input documents & related standards and norms``) is still excluded
    along with its parent.
    """
    roots: dict[str, str] = {}
    for heading in headings:
        rule = _excluded_section_rule(heading)
        if rule is not None:
            roots[heading.number] = rule
    return roots


def is_excluded_section(heading: Heading | None, roots: dict[str, str]) -> str | None:
    """The rule excluding ``heading`` (directly or via an ancestor), or None."""
    if heading is None:
        return None
    for number, rule in roots.items():
        if heading.number == number or heading.number.startswith(f"{number}."):
            return rule
    return None


def subtract_spans(
    start: int, end: int, spans: Sequence[tuple[int, int]]
) -> list[tuple[int, int]]:
    """``[start, end)`` minus every span in ``spans``, in order.

    ``spans`` must be sorted and non-overlapping, which is what a
    non-overlapping ``⌈…⌋`` scan produces.
    """
    residues: list[tuple[int, int]] = []
    cursor = start
    for span_start, span_end in spans:
        if span_end <= cursor or span_start >= end:
            continue
        if span_start > cursor:
            residues.append((cursor, span_start))
        cursor = max(cursor, span_end)
        if cursor >= end:
            break
    if cursor < end:
        residues.append((cursor, end))
    return residues


def _split_to_ceiling(text: str, base: int) -> list[tuple[int, int]]:
    """Split ``text`` into spans no longer than :data:`MAX_CHUNK_CHARS`.

    Cuts at the last sentence boundary that fits; failing that, at the last
    whitespace; failing that, at the ceiling itself (an unbreakable run of
    800+ non-space characters, which the corpus does not contain but which
    must not produce an oversized chunk if it ever appears).

    Measured on the *cleaned* slice, whose length is an upper bound on the
    normalized length, so every emitted chunk is within the ceiling.
    """
    if len(text) <= MAX_CHUNK_CHARS:
        return [(base, base + len(text))]

    cuts = [match.end() for match in SENTENCE_BOUNDARY_PATTERN.finditer(text)]
    spans: list[tuple[int, int]] = []
    position = 0
    while len(text) - position > MAX_CHUNK_CHARS:
        limit = position + MAX_CHUNK_CHARS
        candidates = [cut for cut in cuts if position < cut <= limit]
        if candidates:
            cut = candidates[-1]
        else:
            whitespace = max(text.rfind(" ", position, limit), text.rfind("\n", position, limit))
            cut = whitespace + 1 if whitespace > position else limit
        spans.append((base + position, base + cut))
        position = cut
    if position < len(text):
        spans.append((base + position, base + len(text)))
    return spans


def _pack(units: Sequence[_Unit]) -> list[list[_Unit]]:
    """Group adjacent units into chunks of at most :data:`MAX_CHUNK_CHARS`.

    Flushes once a group reaches :data:`TARGET_CHUNK_CHARS`, so the typical
    chunk sits in the 400–800 band. A trailing group below
    :data:`MIN_CHUNK_CHARS` is folded back into the previous group when it
    fits, and otherwise left for the caller to drop and count.
    """
    groups: list[list[_Unit]] = []
    current: list[_Unit] = []

    for unit in units:
        if current and _joined_length([*current, unit]) > MAX_CHUNK_CHARS:
            groups.append(current)
            current = [unit]
        else:
            current.append(unit)
        if _joined_length(current) >= TARGET_CHUNK_CHARS:
            groups.append(current)
            current = []

    if current:
        folds_back = (
            groups
            and _joined_length(current) < MIN_CHUNK_CHARS
            and _joined_length(groups[-1] + current) <= MAX_CHUNK_CHARS
        )
        if folds_back:
            groups[-1].extend(current)
        else:
            groups.append(current)
    return groups


def _joined_length(units: Sequence[_Unit]) -> int:
    """Length of ``" ".join(unit.text for unit in units)`` without building it."""
    return sum(len(unit.text) for unit in units) + max(len(units) - 1, 0)


def _normalizer(manifest: ProjectManifest, entry: DocumentEntry) -> Callable[[str], str]:
    def normalize(text: str) -> str:
        return normalize_requirement_text(
            text,
            footer_patterns=manifest.extraction.footer_patterns,
            drop_markers=manifest.extraction.drop_markers,
            document_title=entry.title,
        )

    return normalize


def _collect_units(
    document: ParsedDocument,
    extraction: DocumentExtraction,
    cleaned: MappedText,
    normalize: Callable[[str], str],
    stats: ChunkingStats,
) -> list[tuple[Heading | None, list[_Unit]]]:
    """Walk the document's blocks and group prose units per flush point."""
    headings = extraction.headings
    roots = excluded_section_roots(headings)
    body_start = headings[0].source_offset if headings else 0
    indexed_spans = extraction.indexed_spans

    groups: list[tuple[Heading | None, list[_Unit]]] = []
    pending: list[_Unit] = []
    pending_heading: Heading | None = None

    def flush() -> None:
        nonlocal pending, pending_heading
        if pending:
            groups.append((pending_heading, pending))
        pending, pending_heading = [], None

    for _page_number, block in document.iter_blocks():
        stats.blocks_total += 1
        if block.char_start < body_start:
            stats.blocks_front_matter += 1
            flush()
            continue
        if HEADING_BLOCK_PATTERN.match(block.text):
            stats.blocks_headings += 1
            flush()
            continue

        heading = heading_for_offset(headings, block.char_start)
        rule = is_excluded_section(heading, roots)
        if rule is not None:
            stats.blocks_excluded_sections[rule] = stats.blocks_excluded_sections.get(rule, 0) + 1
            flush()
            continue

        block_start = cleaned.cleaned_index(block.char_start)
        block_end = cleaned.cleaned_index(block.char_end)
        residues = subtract_spans(block_start, block_end, indexed_spans)
        if len(residues) != 1 or residues[0] != (block_start, block_end):
            stats.blocks_touching_requirements += 1

        for residue_start, residue_end in residues:
            slice_text = cleaned.text[residue_start:residue_end]
            if not slice_text.strip():
                flush()
                continue
            stats.residues_considered += 1
            if CAPTION_PATTERN.match(normalize(slice_text)):
                stats.blocks_captions += 1
                flush()
                continue
            if pending and residue_start != pending[-1].cleaned_end:
                flush()
            if pending_heading is None:
                pending_heading = heading
            for start, end in _split_to_ceiling(slice_text, residue_start):
                text = normalize(cleaned.text[start:end])
                if text:
                    pending.append(_Unit(start, end, text))

    flush()
    return groups


def extract_context_chunks(
    document: ParsedDocument,
    manifest: ProjectManifest,
    entry: DocumentEntry,
    extraction: DocumentExtraction,
) -> tuple[list[Requirement], ChunkingStats]:
    """Chunk ``document``'s non-requirement prose into ``doc_type="context"``.

    ``extraction`` supplies the cleaned text, its origin map, the detected
    headings and the ``⌈…⌋`` body spans, so the document is cleaned once for
    both stories rather than twice with two chances to diverge.

    Chunk ids are ``CTX_<doc_key>_<section>_<nn>`` — deterministic for a given
    input, which is what lets the embedding cache actually hit on re-ingest.
    """
    cleaned = extraction.cleaned
    normalize = _normalizer(manifest, entry)
    stats = ChunkingStats()
    chunks: list[Requirement] = []
    counters: dict[str, int] = {}

    for heading, units in _collect_units(document, extraction, cleaned, normalize, stats):
        for group in _pack(units):
            text = " ".join(unit.text for unit in group)
            if len(text) < MIN_CHUNK_CHARS:
                stats.chunks_dropped_short += 1
                continue
            source_start, source_end = cleaned.source_span(
                group[0].cleaned_start, group[-1].cleaned_end
            )
            page, bbox = locate_span(document, source_start, source_end)
            section = heading.number if heading else UNSECTIONED
            counters[section] = counters.get(section, 0) + 1
            chunks.append(
                Requirement(
                    id=f"CTX_{entry.key}_{section}_{counters[section]:02d}",
                    title=heading.title if heading else None,
                    text=text,
                    section_path=heading.path if heading else None,
                    page=page,
                    bbox=bbox,
                    char_span=(source_start, source_end),
                    source_doc=entry.filename,
                    upstream_ids=[],
                    named_symbols=extract_named_symbols(
                        text, manifest.extraction.symbol_pattern
                    ),
                    version=manifest.version,
                    project_id=manifest.project_id,
                    doc_type="context",
                )
            )
            stats.chunks_emitted += 1

    return chunks, stats
