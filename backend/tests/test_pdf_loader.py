"""Tests for ingestion/pdf_loader.py (S1.3.2).

The parser's mechanics are exercised against a **synthetic** PDF built here
with PyMuPDF: it is deterministic, it puts known strings at known
coordinates, and it carries no copyright, so it can live in the test suite
where a real AUTOSAR page could not (controller ruling R11). The opt-in
tests at the bottom run against the real corpus and skip when ``data/docs``
has not been fetched.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from core.manifest import load_manifest
from ingestion.fetcher import docs_dir
from ingestion.pdf_loader import EXTRACTION_MODE, PdfParseError, parse_pdf

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST_PATH = REPO_ROOT / "projects" / "autosar-can" / "project.yaml"

PAGE_WIDTH = 400.0
PAGE_HEIGHT = 500.0

# (text, insertion point). One string per page region so each lands in its
# own block, at a y we can assert the bbox against.
PAGE_ONE_TEXTS = [
    ("TOP OF PAGE ONE", (50.0, 80.0)),
    ("MIDDLE OF PAGE ONE", (50.0, 250.0)),
    ("BOTTOM OF PAGE ONE", (50.0, 420.0)),
]
PAGE_TWO_TEXTS = [
    ("TOP OF PAGE TWO", (50.0, 80.0)),
    ("BOTTOM OF PAGE TWO", (50.0, 420.0)),
]

# The brief's ground truth, measured during planning. Keyed by manifest
# document key so widening the corpus again does not break this test.
REAL_PAGE_COUNTS = {
    "can_driver": 131,
    "can_interface": 228,
    "can_transport": 108,
    "can_statemanager": 115,
}


@pytest.fixture(scope="module")
def synthetic_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "synthetic.pdf"
    document = pymupdf.open()
    for texts in (PAGE_ONE_TEXTS, PAGE_TWO_TEXTS):
        page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        for text, point in texts:
            page.insert_text(point, text, fontsize=12)
    document.save(path)
    document.close()
    return path


# --------------------------------------------------------------------------
# pages, blocks, coordinates
# --------------------------------------------------------------------------


def test_page_numbering_is_one_based(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    assert parsed.page_count == 2
    assert [page.page for page in parsed.pages] == [1, 2]
    assert parsed.page(1) is parsed.pages[0]
    assert parsed.page(2).page == 2


def test_page_size_is_reported(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    for page in parsed.pages:
        assert page.width == pytest.approx(PAGE_WIDTH)
        assert page.height == pytest.approx(PAGE_HEIGHT)


def test_blocks_carry_the_known_strings(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    page_one = [block.text.strip() for block in parsed.page(1).blocks]
    page_two = [block.text.strip() for block in parsed.page(2).blocks]
    assert page_one == [text for text, _ in PAGE_ONE_TEXTS]
    assert page_two == [text for text, _ in PAGE_TWO_TEXTS]


def test_block_bboxes_bracket_the_insertion_points(synthetic_pdf: Path):
    """A bbox must be a plausible box around the string it belongs to."""
    parsed = parse_pdf(synthetic_pdf)
    for page, expected in ((parsed.page(1), PAGE_ONE_TEXTS), (parsed.page(2), PAGE_TWO_TEXTS)):
        for block, (text, (x, y)) in zip(page.blocks, expected, strict=True):
            x0, y0, x1, y1 = block.bbox
            assert all(isinstance(v, float) for v in block.bbox)
            assert x0 < x1 and y0 < y1, f"{text}: degenerate bbox {block.bbox}"
            # PyMuPDF's origin is top-left; insert_text places the baseline at
            # y, so the box straddles it.
            assert y0 < y <= y1 + 1.0, f"{text}: bbox {block.bbox} does not contain y={y}"
            assert x0 == pytest.approx(x, abs=2.0), f"{text}: bbox starts at {x0}, not {x}"
            assert 0.0 <= x0 and x1 <= PAGE_WIDTH
            assert 0.0 <= y0 and y1 <= PAGE_HEIGHT
            # Wider strings get wider boxes.
            assert x1 - x0 > len(text) * 3.0


def test_blocks_are_in_top_to_bottom_reading_order(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    tops = [block.bbox[1] for block in parsed.page(1).blocks]
    assert tops == sorted(tops)


# --------------------------------------------------------------------------
# the char-offset index
# --------------------------------------------------------------------------


def test_document_text_is_exactly_the_blocks_concatenated(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    assert parsed.text == "".join(page.text for page in parsed.pages)
    assert parsed.text == "".join(
        block.text for page in parsed.pages for block in page.blocks
    )


def test_every_block_char_span_indexes_its_own_text(synthetic_pdf: Path):
    """The invariant the extractor relies on for its bbox fallback."""
    parsed = parse_pdf(synthetic_pdf)
    for _, block in parsed.iter_blocks():
        assert parsed.text[block.char_start : block.char_end] == block.text


def test_page_char_spans_are_contiguous_and_cover_the_document(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    assert parsed.pages[0].char_start == 0
    for previous, current in zip(parsed.pages, parsed.pages[1:], strict=False):
        assert previous.char_end == current.char_start
    assert parsed.pages[-1].char_end == len(parsed.text)
    for page in parsed.pages:
        assert parsed.text[page.char_start : page.char_end] == page.text


def test_char_offset_maps_back_to_the_right_page(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    offset_one = parsed.text.index("MIDDLE OF PAGE ONE")
    offset_two = parsed.text.index("TOP OF PAGE TWO")

    assert parsed.page_for_char(offset_one) == 1
    assert parsed.page_for_char(offset_two) == 2
    assert parsed.page_for_char(0) == 1
    assert parsed.page_for_char(len(parsed.text)) == 2

    block = parsed.block_for_char(offset_two)
    assert block is not None
    assert block.text.strip() == "TOP OF PAGE TWO"


def test_span_across_the_page_break_reports_both_pages(synthetic_pdf: Path):
    """The SWS_Can_00407 shape: one span, two pages, no single bbox."""
    parsed = parse_pdf(synthetic_pdf)
    start = parsed.text.index("BOTTOM OF PAGE ONE")
    end = parsed.text.index("TOP OF PAGE TWO") + len("TOP OF PAGE TWO")

    assert parsed.pages_for_span(start, end) == [1, 2]
    # A span inside one page stays on that page.
    assert parsed.pages_for_span(start, start + len("BOTTOM OF PAGE ONE")) == [1]


def test_extraction_mode_is_recorded_for_the_fixtures():
    assert EXTRACTION_MODE == "blocks"


# --------------------------------------------------------------------------
# failure modes
# --------------------------------------------------------------------------


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(PdfParseError, match="PDF not found"):
        parse_pdf(tmp_path / "nope.pdf")


def test_non_pdf_file_raises(tmp_path: Path):
    path = tmp_path / "not-really.pdf"
    path.write_bytes(b"just some bytes, definitely not a PDF")
    with pytest.raises(PdfParseError, match="cannot open as PDF"):
        parse_pdf(path)


def test_page_out_of_range_raises(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    with pytest.raises(PdfParseError, match="out of range"):
        parsed.page(0)
    with pytest.raises(PdfParseError, match="out of range"):
        parsed.page(3)


def test_negative_offset_and_reversed_span_raise(synthetic_pdf: Path):
    parsed = parse_pdf(synthetic_pdf)
    with pytest.raises(PdfParseError, match="negative char offset"):
        parsed.page_for_char(-1)
    with pytest.raises(PdfParseError, match="precedes start"):
        parsed.pages_for_span(10, 4)


# --------------------------------------------------------------------------
# opt-in: the real corpus (skipped on a fresh clone with no data/)
# --------------------------------------------------------------------------


def real_documents():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    directory = docs_dir(manifest)
    return manifest, [
        (doc, directory / doc.filename)
        for doc in manifest.documents
        if (directory / doc.filename).is_file()
    ]


@pytest.mark.parametrize("key", sorted(REAL_PAGE_COUNTS))
def test_real_document_page_count(key: str):
    manifest, available = real_documents()
    match = [(doc, path) for doc, path in available if doc.key == key]
    if not match:
        pytest.skip(
            f"{key}: PDF not fetched — run "
            "'uv run python -m ingestion.fetcher ../projects/autosar-can/project.yaml'"
        )
    document, path = match[0]
    parsed = parse_pdf(path)
    assert parsed.page_count == REAL_PAGE_COUNTS[key], (
        f"{document.filename}: expected {REAL_PAGE_COUNTS[key]} pages, got {parsed.page_count} — "
        "the upstream document may have been republished"
    )
    assert parsed.pages[0].page == 1
    assert parsed.pages[-1].page == parsed.page_count
    assert parsed.text
    assert manifest.extraction.body_open in parsed.text


def test_real_document_block_invariants_hold_at_scale():
    _, available = real_documents()
    if not available:
        pytest.skip("no corpus PDFs fetched")
    document, path = available[0]
    parsed = parse_pdf(path)
    for page_number, block in parsed.iter_blocks():
        assert parsed.text[block.char_start : block.char_end] == block.text
        assert parsed.page_for_char(block.char_start) == page_number
        x0, y0, x1, y1 = block.bbox
        assert x0 <= x1 and y0 <= y1
