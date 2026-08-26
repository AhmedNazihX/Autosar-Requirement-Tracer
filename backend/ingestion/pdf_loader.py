"""PyMuPDF page/block/coordinate extraction — no requirement knowledge.

Story S1.3.2. This module knows about pages, text blocks, characters and
coordinates. It deliberately knows *nothing* about requirements: no
``SWS_`` regex, no ``⌈``/``⌋`` ceilings, no page-furniture stripping. All of
that lives in the extractor (story S1.3.3), driven by the manifest's
``extraction`` patterns. Keeping the boundary here is what makes the corpus
swappable, which the design spec (§2) calls out as a goal.

Two location mechanisms are exposed because the extractor needs both:

* **bbox** — usable when a requirement sits inside a single block.
* **char_span** into :attr:`ParsedDocument.text` — the fallback for a
  requirement whose body spans a page break (e.g. ``SWS_Can_00407``), where
  no single bbox is meaningful.

The two are kept consistent by construction: the document text is exactly
the concatenation of the block texts in extraction order, so for every block
``document.text[block.char_start:block.char_end] == block.text`` holds. The
extraction mode is PyMuPDF's block level (``page.get_text("blocks")``).
Image blocks are dropped — they carry no text.

Everything here is a pure function of a path. No caching, no global state.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

#: Recorded in golden fixtures so a parser upgrade that changes extraction is
#: visible in the diff rather than silent.
EXTRACTION_MODE = "blocks"

#: PyMuPDF's block-type discriminator in ``get_text("blocks")`` tuples.
_BLOCK_TYPE_TEXT = 0


class PdfParseError(Exception):
    """Raised when a PDF cannot be opened or a requested page does not exist."""


@dataclass(frozen=True)
class TextBlock:
    """One PyMuPDF text block, with its coordinates and its char span.

    ``bbox`` is ``(x0, y0, x1, y1)`` in PDF points, origin top-left (PyMuPDF's
    convention). ``char_start``/``char_end`` index
    :attr:`ParsedDocument.text`.
    """

    text: str
    bbox: tuple[float, float, float, float]
    block_no: int
    char_start: int
    char_end: int

    def contains_char(self, offset: int) -> bool:
        return self.char_start <= offset < self.char_end


@dataclass(frozen=True)
class ParsedPage:
    """One page: its 1-based number, its size, its text blocks and its text."""

    page: int
    width: float
    height: float
    blocks: list[TextBlock] = field(default_factory=list)
    text: str = ""
    char_start: int = 0
    char_end: int = 0

    def block_for_char(self, offset: int) -> TextBlock | None:
        """The block containing document char ``offset``, if any."""
        for block in self.blocks:
            if block.contains_char(offset):
                return block
        return None


@dataclass(frozen=True)
class ParsedDocument:
    """A whole parsed PDF: pages with blocks, plus the concatenated text.

    ``text`` is the concatenation of every page's text in page order, which is
    itself the concatenation of that page's block texts. That makes char
    offsets from a regex over ``text`` mappable back to a page (and usually a
    block) without re-extracting anything.
    """

    filename: str
    path: str
    page_count: int
    pages: list[ParsedPage]
    text: str

    @property
    def _page_starts(self) -> list[int]:
        # Rebuilt per call rather than cached: the dataclass is frozen and the
        # list is one int per page (a few hundred at most), so this is cheaper
        # than working around the immutability.
        return [page.char_start for page in self.pages]

    def page(self, number: int) -> ParsedPage:
        """The page with 1-based ``number``."""
        if not 1 <= number <= len(self.pages):
            raise PdfParseError(
                f"{self.filename}: page {number} out of range (1..{len(self.pages)})"
            )
        return self.pages[number - 1]

    def page_for_char(self, offset: int) -> int:
        """1-based page number containing document char ``offset``.

        Offsets are clamped to the document, so the end-of-text offset maps to
        the last page rather than raising.
        """
        if not self.pages:
            raise PdfParseError(f"{self.filename}: document has no pages")
        if offset < 0:
            raise PdfParseError(f"{self.filename}: negative char offset {offset}")
        index = bisect.bisect_right(self._page_starts, offset) - 1
        index = min(max(index, 0), len(self.pages) - 1)
        return self.pages[index].page

    def pages_for_span(self, start: int, end: int) -> list[int]:
        """Every 1-based page number a ``[start, end)`` char span touches.

        A span covering more than one page is the page-break case: the
        extractor records ``char_span`` and leaves ``bbox`` as ``None``.
        """
        if end < start:
            raise PdfParseError(f"{self.filename}: span end {end} precedes start {start}")
        first = self.page_for_char(start)
        last = self.page_for_char(max(end - 1, start))
        return list(range(first, last + 1))

    def block_for_char(self, offset: int) -> TextBlock | None:
        """The block containing document char ``offset``, if any."""
        return self.page(self.page_for_char(offset)).block_for_char(offset)

    def iter_blocks(self) -> Iterator[tuple[int, TextBlock]]:
        """Yield ``(page_number, block)`` across the whole document."""
        for page in self.pages:
            for block in page.blocks:
                yield page.page, block


def _with_trailing_newline(text: str) -> str:
    """PyMuPDF block text normally ends in ``\\n``; guarantee it.

    Without the guarantee, concatenating block texts could fuse the last word
    of one block onto the first word of the next.
    """
    return text if text.endswith("\n") else text + "\n"


def parse_pdf(path: str | Path) -> ParsedDocument:
    """Parse every page of the PDF at ``path`` into blocks with coordinates."""
    pdf_path = Path(path)
    if not pdf_path.is_file():
        raise PdfParseError(f"PDF not found: {pdf_path}")

    pages: list[ParsedPage] = []
    chunks: list[str] = []
    offset = 0

    try:
        document = pymupdf.open(pdf_path)
    except Exception as exc:  # pymupdf raises assorted types for bad input
        raise PdfParseError(f"{pdf_path}: cannot open as PDF — {exc}") from exc

    with document:
        for index, page in enumerate(document):
            page_start = offset
            blocks: list[TextBlock] = []
            for raw in page.get_text(EXTRACTION_MODE):
                x0, y0, x1, y1, raw_text, block_no, block_type = raw[:7]
                if block_type != _BLOCK_TYPE_TEXT or not raw_text:
                    continue
                text = _with_trailing_newline(raw_text)
                blocks.append(
                    TextBlock(
                        text=text,
                        bbox=(float(x0), float(y0), float(x1), float(y1)),
                        block_no=int(block_no),
                        char_start=offset,
                        char_end=offset + len(text),
                    )
                )
                chunks.append(text)
                offset += len(text)
            pages.append(
                ParsedPage(
                    page=index + 1,
                    width=float(page.rect.width),
                    height=float(page.rect.height),
                    blocks=blocks,
                    text="".join(block.text for block in blocks),
                    char_start=page_start,
                    char_end=offset,
                )
            )

    return ParsedDocument(
        filename=pdf_path.name,
        path=str(pdf_path),
        page_count=len(pages),
        pages=pages,
        text="".join(chunks),
    )
