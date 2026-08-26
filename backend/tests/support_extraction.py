"""Helpers for driving the extractor without a PDF.

Two builders, shared by ``test_req_extractor.py`` and
``test_context_chunks.py``:

* :func:`document_from_golden` rebuilds a
  :class:`~ingestion.pdf_loader.ParsedDocument` from the committed
  ``*.blocks.json`` fixtures, so the extractor can be measured against the
  hand-labeled ``*.expected.json`` on a fresh clone with no ``data/``
  directory (controller ruling R11).
* :func:`document_from_blocks` builds a one-page document from literal block
  texts, for the cases no fixture page happens to contain (an upstream
  ``RS_*`` id, a table-of-contents line, a caption).

The golden builder rebases char offsets to zero and pads the page list with
empty pages up to the fixture's page number, because ``ParsedDocument.page()``
indexes ``pages[number - 1]``. Rebasing keeps ``page_for_char`` and
``pages_for_span`` behaving exactly as they do on the real document — which is
the part the page-break case depends on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ingestion.pdf_loader import ParsedDocument, ParsedPage, TextBlock

GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"

#: Nominal A4-ish page box for synthetic documents.
SYNTHETIC_PAGE_WIDTH = 595.0
SYNTHETIC_PAGE_HEIGHT = 842.0


def load_golden(fixture: str, half: str) -> dict[str, Any]:
    """Load ``<fixture>.<half>.json`` from the golden directory."""
    return json.loads((GOLDEN_DIR / f"{fixture}.{half}.json").read_text(encoding="utf-8"))


def golden_index() -> list[dict[str, Any]]:
    """The frozen fixture list from ``fixtures.json``."""
    return json.loads((GOLDEN_DIR / "fixtures.json").read_text(encoding="utf-8"))["pages"]


def following_fixture(entry: dict[str, Any], index: list[dict[str, Any]]) -> str | None:
    """The fixture for the page immediately after ``entry``, if one is frozen.

    A requirement whose body crosses a page break is only complete across the
    pair, so the extractor has to be handed both halves — the same rule
    ``test_golden_fixtures.py`` applies when it searches for labeled text.
    """
    for candidate in index:
        if (
            candidate["document_key"] == entry["document_key"]
            and candidate["page"] == entry["page"] + 1
        ):
            return candidate["fixture"]
    return None


def document_from_golden(*fixtures: str) -> ParsedDocument:
    """Rebuild a ``ParsedDocument`` from one or more ``*.blocks.json`` files.

    The fixtures must be consecutive pages of the same document, in order.
    """
    if not fixtures:
        raise ValueError("at least one fixture is required")

    loaded = [load_golden(fixture, "blocks") for fixture in fixtures]
    sources = {payload["source_document"] for payload in loaded}
    if len(sources) != 1:
        raise ValueError(f"fixtures span more than one document: {sorted(sources)}")

    highest_page = max(payload["page"] for payload in loaded)
    by_page = {payload["page"]: payload for payload in loaded}

    pages: list[ParsedPage] = []
    chunks: list[str] = []
    offset = 0
    for number in range(1, highest_page + 1):
        payload = by_page.get(number)
        if payload is None:
            # Filler: no blocks, no text, zero-width char span. Keeps
            # pages[number - 1] addressable without inventing content.
            pages.append(
                ParsedPage(
                    page=number,
                    width=SYNTHETIC_PAGE_WIDTH,
                    height=SYNTHETIC_PAGE_HEIGHT,
                    blocks=[],
                    text="",
                    char_start=offset,
                    char_end=offset,
                )
            )
            continue

        page_start = offset
        blocks: list[TextBlock] = []
        for block in payload["blocks"]:
            text = block["text"]
            blocks.append(
                TextBlock(
                    text=text,
                    bbox=tuple(float(v) for v in block["bbox"]),  # type: ignore[arg-type]
                    block_no=block["block_no"],
                    char_start=offset,
                    char_end=offset + len(text),
                )
            )
            chunks.append(text)
            offset += len(text)
        pages.append(
            ParsedPage(
                page=number,
                width=float(payload["width"]),
                height=float(payload["height"]),
                blocks=blocks,
                text="".join(block.text for block in blocks),
                char_start=page_start,
                char_end=offset,
            )
        )

    return ParsedDocument(
        filename=sources.pop(),
        path=f"<golden:{'+'.join(fixtures)}>",
        page_count=len(pages),
        pages=pages,
        text="".join(chunks),
    )


def document_from_blocks(
    block_texts: list[str], *, filename: str = "synthetic.pdf", pages: int = 1
) -> ParsedDocument:
    """Build a document from literal block texts, split evenly over ``pages``.

    Each block text is newline-terminated if it is not already, matching what
    ``ingestion.pdf_loader`` guarantees.
    """
    if pages < 1:
        raise ValueError("pages must be at least 1")

    normalized = [text if text.endswith("\n") else text + "\n" for text in block_texts]
    per_page = -(-len(normalized) // pages)  # ceil
    parsed_pages: list[ParsedPage] = []
    offset = 0
    for page_index in range(pages):
        page_start = offset
        blocks: list[TextBlock] = []
        slice_ = normalized[page_index * per_page : (page_index + 1) * per_page]
        for block_no, text in enumerate(slice_):
            top = 60.0 + block_no * 40.0
            blocks.append(
                TextBlock(
                    text=text,
                    bbox=(71.0, top, 524.0, top + 30.0),
                    block_no=block_no,
                    char_start=offset,
                    char_end=offset + len(text),
                )
            )
            offset += len(text)
        parsed_pages.append(
            ParsedPage(
                page=page_index + 1,
                width=SYNTHETIC_PAGE_WIDTH,
                height=SYNTHETIC_PAGE_HEIGHT,
                blocks=blocks,
                text="".join(block.text for block in blocks),
                char_start=page_start,
                char_end=offset,
            )
        )

    return ParsedDocument(
        filename=filename,
        path=f"<synthetic:{filename}>",
        page_count=len(parsed_pages),
        pages=parsed_pages,
        text="".join(page.text for page in parsed_pages),
    )
