"""``data/extraction_report.md`` — the visible evidence for extraction.

The plan commits to *deterministic* requirement extraction with no LLM in the
loop. This report is what makes that decision auditable: it states, per
document, how many requirements were found against the manifest's
``expected_requirements``, how many carry each optional field, and — the part
that matters — **every body opener that produced no requirement**, with
enough surrounding text for a human to judge whether it was a real
requirement or correctly skipped. A silent count is not evidence; a named
miss list is.

Written on every ingestion run, under ``data/`` (gitignored).

Running it directly is a stopgap until the ingestion CLI (story S1.3.6) lands
and calls :func:`write_extraction_report` as one stage of the pipeline::

    cd backend
    uv run python -m ingestion.extraction_report ../projects/autosar-can/project.yaml
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.manifest import DocumentEntry, ProjectManifest, load_manifest
from core.models import Requirement
from ingestion.context_chunker import (
    EXCLUDED_SECTION_RULES,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    ChunkingStats,
    extract_context_chunks,
)
from ingestion.pdf_loader import parse_pdf
from ingestion.req_extractor import DocumentExtraction, extract_requirements

#: How many miss-context characters to show per miss in the report.
MISS_QUOTE_CHARS = 320


class ExtractionReportError(Exception):
    """Raised when the report cannot be produced (missing PDF, bad paths)."""


@dataclass(frozen=True)
class DocumentIngestion:
    """One document's extraction plus its context chunks."""

    entry: DocumentEntry
    extraction: DocumentExtraction
    context_chunks: list[Requirement]
    chunk_stats: ChunkingStats


def ingest_document(
    manifest: ProjectManifest, entry: DocumentEntry, pdf_path: Path
) -> DocumentIngestion:
    """Parse, extract requirements from, and chunk one document."""
    if not pdf_path.is_file():
        raise ExtractionReportError(
            f"{pdf_path} is missing — run "
            "'uv run python -m ingestion.fetcher ../projects/autosar-can/project.yaml' first"
        )
    document = parse_pdf(pdf_path)
    extraction = extract_requirements(document, manifest, entry)
    chunks, stats = extract_context_chunks(document, manifest, entry, extraction)
    return DocumentIngestion(entry, extraction, chunks, stats)


def data_dir(manifest: ProjectManifest) -> Path:
    """The repo's ``data/`` directory, resolved from the manifest's location."""
    root = manifest.project_root
    if root is None:
        raise ExtractionReportError(
            "manifest has no project_root (constructed directly instead of via "
            "load_manifest?) — pass an explicit output path"
        )
    return root / "data"


def _pct(count: int, total: int) -> str:
    return f"{100.0 * count / total:.1f}%" if total else "n/a"


def _miss_lines(manifest: ProjectManifest, ingestion: DocumentIngestion) -> list[str]:
    """Render the miss list, saying *why* each nearest id failed to match."""
    id_pattern = re.compile(rf"\A{manifest.extraction.req_id_pattern}\Z")
    misses = ingestion.extraction.misses
    if not misses:
        return ["No unmatched body openers: every `⌈` on every page produced a requirement."]

    lines = [
        f"{len(misses)} body opener(s) produced no requirement. Each is listed with the last "
        "bracketed token before it — in this corpus that is the id that failed "
        "`extraction.req_id_pattern`.",
        "",
    ]
    for miss in misses:
        token = miss.nearest_bracketed
        if token is None:
            verdict = "no bracketed token precedes it"
        elif id_pattern.match(token):
            verdict = (
                f"`{token}` **does** match `req_id_pattern` — investigate, this is not a "
                "structural exclusion"
            )
        else:
            verdict = f"`{token}` does not match `req_id_pattern`"
        lines.append(f"- **page {miss.page}** (char {miss.source_offset}) — {verdict}")
        lines.append(f"  > {miss.context[:MISS_QUOTE_CHARS]}")
    return lines


def _document_section(manifest: ProjectManifest, ingestion: DocumentIngestion) -> list[str]:
    entry, extraction = ingestion.entry, ingestion.extraction
    requirements = extraction.requirements
    total = len(requirements)
    delta = total - entry.expected_requirements
    stats = ingestion.chunk_stats

    titled = sum(1 for r in requirements if r.title)
    with_refs = sum(1 for r in requirements if r.upstream_ids)
    with_symbols = sum(1 for r in requirements if r.named_symbols)
    with_bbox = sum(1 for r in requirements if r.bbox)
    with_section = sum(1 for r in requirements if r.section_path)
    symbols = sorted({symbol for r in requirements for symbol in r.named_symbols})
    upstream = sorted({up for r in requirements for up in r.upstream_ids})

    lines = [
        f"## {entry.key} — {entry.title}",
        "",
        f"- source: `{entry.filename}` ({extraction.page_count} pages), module `{entry.module}`",
        f"- body openers (`{manifest.extraction.body_open}`) on the cleaned text: "
        f"{extraction.body_opener_count}",
        f"- **requirements extracted: {total}** against `expected_requirements` "
        f"{entry.expected_requirements} (delta {delta:+d})",
        f"- with a title: {titled} ({_pct(titled, total)})",
        f"- with upstream ids: {with_refs} ({_pct(with_refs, total)})",
        f"- with named symbols: {with_symbols} ({_pct(with_symbols, total)}), "
        f"{len(symbols)} distinct",
        f"- with a resolved bbox: {with_bbox} ({_pct(with_bbox, total)}) — the remaining "
        f"{total - with_bbox} span a page break and carry `char_span` only",
        f"- with a section path: {with_section} ({_pct(with_section, total)}), "
        f"{len(extraction.headings)} headings detected",
        f"- distinct upstream ids referenced: {len(upstream)}",
        "",
    ]

    if extraction.warnings:
        lines.append("### Warnings")
        lines.append("")
        lines.extend(f"- {warning}" for warning in extraction.warnings)
        lines.append("")

    lines.append("### Misses")
    lines.append("")
    lines.extend(_miss_lines(manifest, ingestion))
    lines.append("")

    lines.append("### Context chunks")
    lines.append("")
    chunk_lengths = [len(chunk.text) for chunk in ingestion.context_chunks]
    if chunk_lengths:
        lines.append(
            f"- emitted: {len(chunk_lengths)} (lengths {min(chunk_lengths)}–"
            f"{max(chunk_lengths)}, mean {sum(chunk_lengths) // len(chunk_lengths)}; "
            f"bounds {MIN_CHUNK_CHARS}–{MAX_CHUNK_CHARS})"
        )
    else:
        lines.append("- emitted: 0")
    lines.extend(
        [
            f"- blocks seen: {stats.blocks_total}",
            f"- dropped, front matter before the first numbered heading "
            f"(title page, disclaimer, change-history table, table of contents, lists of "
            f"figures and tables): {stats.blocks_front_matter} blocks",
            f"- dropped, heading blocks (re-indexed as chunk metadata instead): "
            f"{stats.blocks_headings} blocks",
            f"- dropped, figure/table captions: {stats.blocks_captions} blocks",
            f"- dropped, excluded sections: {stats.blocks_excluded_sections_total} blocks "
            f"({stats.blocks_excluded_sections or 'none'})",
            f"- blocks a requirement span cut into: {stats.blocks_touching_requirements}",
            f"- prose residues considered: {stats.residues_considered}",
            f"- dropped, below {MIN_CHUNK_CHARS} characters: {stats.chunks_dropped_short} chunks",
            "",
        ]
    )

    lines.append("### Distinct named symbols")
    lines.append("")
    lines.append(", ".join(f"`{symbol}`" for symbol in symbols) if symbols else "none")
    lines.append("")
    return lines


def render_extraction_report(
    manifest: ProjectManifest, results: list[DocumentIngestion]
) -> str:
    """Render the whole report as Markdown."""
    total_reqs = sum(len(r.extraction.requirements) for r in results)
    total_expected = sum(r.entry.expected_requirements for r in results)
    total_chunks = sum(len(r.context_chunks) for r in results)
    total_openers = sum(r.extraction.body_opener_count for r in results)
    total_misses = sum(len(r.extraction.misses) for r in results)
    symbol_counts = Counter(
        symbol
        for result in results
        for requirement in result.extraction.requirements
        for symbol in requirement.named_symbols
    )

    lines = [
        "# Extraction report",
        "",
        f"- project: `{manifest.project_id}` ({manifest.name}), corpus version "
        f"`{manifest.version}`",
        f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- documents: {len(results)}",
        "",
        "## Summary",
        "",
        "| document | pages | openers | requirements | expected | delta | misses "
        "| context chunks |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for result in results:
        extraction = result.extraction
        delta = len(extraction.requirements) - result.entry.expected_requirements
        lines.append(
            f"| {result.entry.key} | {extraction.page_count} "
            f"| {extraction.body_opener_count} | {len(extraction.requirements)} "
            f"| {result.entry.expected_requirements} | {delta:+d} "
            f"| {len(extraction.misses)} | {len(result.context_chunks)} |"
        )
    lines.extend(
        [
            f"| **total** | | {total_openers} | **{total_reqs}** | {total_expected} "
            f"| {total_reqs - total_expected:+d} | {total_misses} | {total_chunks} |",
            "",
            f"Distinct named symbols corpus-wide: {len(symbol_counts)}. Most frequent: "
            + ", ".join(f"`{s}` ({n})" for s, n in symbol_counts.most_common(10)),
            "",
            "### Patterns used (all from the manifest, none hard-coded)",
            "",
            f"- `extraction.req_id_pattern`: `{manifest.extraction.req_id_pattern}`",
            f"- `extraction.upstream_id_pattern`: "
            f"`{manifest.extraction.upstream_id_pattern}`",
            f"- `extraction.symbol_pattern`: `{manifest.extraction.symbol_pattern}`",
            f"- `extraction.body_open` / `body_close`: "
            f"`{manifest.extraction.body_open}` / `{manifest.extraction.body_close}`",
            "- `extraction.footer_patterns`: "
            + ", ".join(f"`{p}`" for p in manifest.extraction.footer_patterns),
            "- `extraction.drop_markers`: "
            + ", ".join(f"`{m}`" for m in manifest.extraction.drop_markers),
            "",
            "### Context-chunk exclusion rules",
            "",
            f"- chunk size: {MIN_CHUNK_CHARS}–{MAX_CHUNK_CHARS} characters, split on "
            "paragraph (PyMuPDF block) boundaries, never mid-sentence",
            "- requirement blocks (`[id] title ⌈body⌋(refs)`) are subtracted before "
            "chunking, so no character is indexed twice",
            "- excluded sections, by heading title (with all descendants): "
            + ", ".join(f"{name} = `{p.pattern}`" for name, p in EXCLUDED_SECTION_RULES.items()),
            "",
        ]
    )

    for result in results:
        lines.extend(_document_section(manifest, result))
    return "\n".join(lines).rstrip() + "\n"


def write_extraction_report(
    manifest: ProjectManifest, results: list[DocumentIngestion], path: Path | None = None
) -> Path:
    """Render the report and write it to ``data/extraction_report.md``."""
    destination = path or (data_dir(manifest) / "extraction_report.md")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(render_extraction_report(manifest, results), encoding="utf-8")
    return destination


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(
            "usage: python -m ingestion.extraction_report <path/to/project.yaml>",
            file=sys.stderr,
        )
        return 2

    manifest = load_manifest(argv[0])
    pdf_dir = data_dir(manifest) / "docs"
    results = [
        ingest_document(manifest, entry, pdf_dir / entry.filename)
        for entry in manifest.documents
    ]
    for result in results:
        print(
            f"{result.entry.key:18s} {len(result.extraction.requirements):5d} requirements "
            f"(expected {result.entry.expected_requirements}), "
            f"{len(result.context_chunks):5d} context chunks, "
            f"{len(result.extraction.misses)} miss(es)"
        )
        for warning in result.extraction.warnings:
            print(f"  WARNING: {warning}", file=sys.stderr)
    destination = write_extraction_report(manifest, results)
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
