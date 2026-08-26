"""Tests for offset-preserving normalization (``ingestion.mapped_text``).

The load-bearing test is
:func:`test_cleaning_agrees_with_text_norm_on_the_golden_pages`: the extractor
locates matches in cleaned coordinates but must report raw ones, so it needs a
cleaner that keeps an origin index — and that cleaner must produce *exactly*
the text ``ingestion.text_norm`` produces, or the hand-labeled golden
fixtures stop being the contract. Rather than trusting the duplication, the
equality is asserted on the real frozen page output.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.manifest import load_manifest
from ingestion.mapped_text import MappedText, clean_with_offsets
from ingestion.text_norm import NormalizationError, collapse_whitespace, normalize_requirement_text
from tests.support_extraction import golden_index, load_golden

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
DOCUMENTS = {doc.key: doc for doc in MANIFEST.documents}
EXTRACTION = MANIFEST.extraction
INDEX = golden_index()


def clean(text: str, title: str) -> MappedText:
    return clean_with_offsets(
        text,
        footer_patterns=EXTRACTION.footer_patterns,
        drop_markers=EXTRACTION.drop_markers,
        document_title=title,
    )


def normalize(text: str, title: str) -> str:
    return normalize_requirement_text(
        text,
        footer_patterns=EXTRACTION.footer_patterns,
        drop_markers=EXTRACTION.drop_markers,
        document_title=title,
    )


@pytest.mark.parametrize("fixture", [entry["fixture"] for entry in INDEX])
def test_cleaning_agrees_with_text_norm_on_the_golden_pages(fixture: str):
    """Rules 1–3 with origins, then rule 4, == the canonical four rules."""
    entry = next(e for e in INDEX if e["fixture"] == fixture)
    title = DOCUMENTS[entry["document_key"]].title
    raw = "".join(block["text"] for block in load_golden(fixture, "blocks")["blocks"])

    assert collapse_whitespace(clean(raw, title).text) == normalize(raw, title)


def test_cleaning_agrees_with_text_norm_across_a_page_break():
    """The pair is the case the footer/header substitution exists for."""
    title = DOCUMENTS["can_driver"].title
    raw = "".join(
        block["text"]
        for fixture in ("can_driver_p034", "can_driver_p035")
        for block in load_golden(fixture, "blocks")["blocks"]
    )
    cleaned = clean(raw, title)

    assert collapse_whitespace(cleaned.text) == normalize(raw, title)
    assert "Document ID 11" not in cleaned.text, "the running footer must be gone"
    assert "driver.Specification" not in cleaned.text, "words fused across the page break"
    # Page 35's header is stripped; page 34's leading one has no preceding
    # newline to anchor the pattern, so it survives as a harmless first line
    # (it is its own block and cannot fuse into a requirement body).
    assert raw.count("AUTOSAR CP R23-11") == 2
    assert cleaned.text.count("AUTOSAR CP R23-11") == 1
    assert cleaned.text.startswith("Specification of CAN Driver\nAUTOSAR CP R23-11\n")


def test_origins_map_cleaned_characters_back_to_the_raw_text():
    raw = "alpha\n1 of 9\nDocument ID 11: DOC\nbeta gam-\nma\n"
    cleaned = clean(raw, "Specification of CAN Driver")

    assert cleaned.text == "alpha\nbeta gamma\n"
    assert len(cleaned.origins) == len(cleaned.text)
    assert cleaned.source_length == len(raw)
    for index, character in enumerate(cleaned.text):
        origin = cleaned.origins[index]
        assert raw[origin] == character, f"index {index}: {character!r} vs raw {raw[origin]!r}"


def test_source_span_round_trips_a_substring():
    raw = "alpha\n1 of 9\nDocument ID 11: DOC\nbeta gam-\nma delta\n"
    cleaned = clean(raw, "Specification of CAN Driver")
    start = cleaned.text.index("gamma")
    source_start, source_end = cleaned.source_span(start, start + len("gamma delta"))

    assert raw[source_start:source_end] == "gam-\nma delta"


def test_origins_are_non_decreasing_so_cleaned_index_is_a_binary_search():
    raw = "a\n1 of 9\nDocument ID 11: D\n▽ b-\nc △ d\n"
    cleaned = clean(raw, "Specification of CAN Driver")
    origins = list(cleaned.origins)
    assert origins == sorted(origins)
    for index in range(len(cleaned.text)):
        assert cleaned.cleaned_index(cleaned.origins[index]) <= index


def test_cleaned_index_maps_a_deleted_offset_to_where_the_run_began():
    raw = "alpha\n1 of 9\nDocument ID 11: DOC\nbeta\n"
    cleaned = clean(raw, "Specification of CAN Driver")
    deleted = raw.index("Document ID")
    assert cleaned.cleaned_index(deleted) == cleaned.text.index("beta")


def test_markers_become_a_space_so_they_cannot_fuse_words():
    cleaned = clean("one▽two\n", "Specification of CAN Driver")
    assert cleaned.text == "one two\n"


def test_span_endpoints_are_validated():
    cleaned = clean("abc\n", "Specification of CAN Driver")
    assert cleaned.source_span(2, 2) == (2, 2)
    assert cleaned.source_offset(len(cleaned.text)) == cleaned.source_length
    with pytest.raises(ValueError):
        cleaned.source_span(3, 1)
    with pytest.raises(IndexError):
        cleaned.source_offset(99)


def test_a_mismatched_origin_index_is_rejected_at_construction():
    from array import array

    with pytest.raises(ValueError, match="origin index"):
        MappedText("abc", array("l", [0, 1]), 3)


def test_a_title_bearing_footer_pattern_without_a_title_is_an_error():
    """Hardening: the empty title used to degrade the pattern silently."""
    with pytest.raises(NormalizationError, match="document_title"):
        clean_with_offsets("x\n", footer_patterns=EXTRACTION.footer_patterns, document_title="")
