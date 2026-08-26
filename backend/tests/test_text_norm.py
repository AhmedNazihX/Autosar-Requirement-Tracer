"""Tests for the normalization contract's two hardening fixes.

``ingestion.text_norm`` is the ratified single source of truth for requirement
text normalization; ``test_golden_fixtures.py`` already exercises its *output*
against the hand labels. What is tested here is the two footguns a re-review
flagged and that D4, as the module's first real consumer, fixed:

1. an empty ``document_title`` used to silently degrade a ``{title}``-bearing
   footer pattern into one that still matched some pages, leaving a
   half-stripped document and no signal;
2. :func:`~ingestion.text_norm.extract_named_symbols` used to index
   ``group(1)`` unconditionally, which returns ``None`` — and appended
   ``None`` to the result — for a pattern whose first group is optional and
   did not participate.

The normalization *semantics* are deliberately not re-specified here; the
fixtures own that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from core.manifest import load_manifest
from ingestion.text_norm import (
    TITLE_PLACEHOLDER,
    NormalizationError,
    expand_footer_pattern,
    extract_named_symbols,
    normalize_requirement_text,
    strip_page_furniture,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
EXTRACTION = MANIFEST.extraction
TITLE = MANIFEST.documents[0].title

#: The pattern that made the empty-title bug reachable.
TITLE_PATTERN = next(p for p in EXTRACTION.footer_patterns if TITLE_PLACEHOLDER in p)


# --------------------------------------------------------------------------
# hardening 1: an empty document_title is an error, not a degradation
# --------------------------------------------------------------------------


def test_expand_footer_pattern_substitutes_and_escapes_the_title():
    expanded = expand_footer_pattern(TITLE_PATTERN, "Spec. of CAN (Driver)")
    assert TITLE_PLACEHOLDER not in expanded
    assert r"Spec\.\ of\ CAN\ \(Driver\)" in expanded or "Spec\\. of CAN \\(Driver\\)" in expanded


def test_a_pattern_without_the_placeholder_needs_no_title():
    plain = next(p for p in EXTRACTION.footer_patterns if TITLE_PLACEHOLDER not in p)
    assert expand_footer_pattern(plain, "") == plain


@pytest.mark.parametrize("title", ["", "   ", "\n\t"])
def test_an_empty_title_raises_instead_of_degrading_the_pattern(title: str):
    with pytest.raises(NormalizationError) as exc:
        expand_footer_pattern(TITLE_PATTERN, title)
    assert TITLE_PLACEHOLDER in str(exc.value)
    assert "documents[].title" in str(exc.value)


def test_the_degradation_it_replaces_would_have_been_invisible():
    """Why the silent degradation was dangerous rather than merely untidy.

    With the title removed, the pattern becomes ``\\n\\nAUTOSAR CP R23-11\\n``,
    which never matches this corpus: the header block still carries the title
    line. So the old behaviour stripped *nothing*, left the running header in
    the text, and fused the last word of one page onto the first word of the
    next — with no error anywhere. Raising is the only way a caller finds out.
    """
    degraded = re.compile(TITLE_PATTERN.replace(TITLE_PLACEHOLDER, ""))
    page_break = f"...the CAN driver.\n{TITLE}\nAUTOSAR CP R23-11\nSpecification continues.\n"

    assert degraded.search(page_break) is None
    assert re.compile(expand_footer_pattern(TITLE_PATTERN, TITLE)).search(page_break) is not None


def test_strip_page_furniture_and_normalize_both_raise_on_a_missing_title():
    with pytest.raises(NormalizationError):
        strip_page_furniture("x\n", EXTRACTION.footer_patterns, "")
    with pytest.raises(NormalizationError):
        normalize_requirement_text(
            "x\n", footer_patterns=EXTRACTION.footer_patterns, document_title=""
        )


def test_no_footer_patterns_means_no_title_is_needed():
    """Normalizing a span known to contain no page break stays a valid call."""
    assert normalize_requirement_text("a  b\n", drop_markers=EXTRACTION.drop_markers) == "a b"


def test_the_real_title_still_strips_the_running_header():
    raw = "tail of a paragraph\n" + f"{TITLE}\nAUTOSAR CP R23-11\n" + "head of the next page\n"
    assert normalize_requirement_text(
        raw, footer_patterns=EXTRACTION.footer_patterns, document_title=TITLE
    ) == "tail of a paragraph head of the next page"


# --------------------------------------------------------------------------
# hardening 2: extract_named_symbols never emits None
# --------------------------------------------------------------------------


def test_the_manifest_symbol_pattern_still_returns_group_one():
    symbols = extract_named_symbols(
        "The function Can_Write calls CanIf_TxConfirmation and then Can_Write again.",
        EXTRACTION.symbol_pattern,
    )
    assert symbols == ["Can_Write", "CanIf_TxConfirmation"]


def test_a_pattern_with_no_groups_falls_back_to_the_whole_match():
    assert extract_named_symbols("Can_Write and Can_Init", r"Can_[A-Z][A-Za-z]*") == [
        "Can_Write",
        "Can_Init",
    ]


def test_a_non_participating_optional_first_group_never_yields_none():
    """The regression the fix exists for.

    ``(Det_)?(Can_\\w+)`` leaves group 1 unmatched for ``Can_Write``. Indexing
    ``group(1)`` returned ``None``, which was then appended to the result list
    and would have reached the ``Requirement.named_symbols`` field.
    """
    symbols = extract_named_symbols(
        "Det_ReportError is called, and so is Can_Write.", r"(Det_)?(Can_[A-Za-z]+)"
    )
    assert None not in symbols
    assert all(isinstance(symbol, str) and symbol for symbol in symbols)
    assert symbols == ["Can_Write"], "the first participating group wins"


def test_a_trailing_optional_group_is_not_ignored():
    """The old code also silently dropped groups 2 and beyond."""
    symbols = extract_named_symbols("prefix Can_Init suffix", r"prefix (Can_[A-Za-z]+)( suffix)?")
    assert symbols == ["Can_Init"]


def test_an_empty_participating_group_is_skipped_not_emitted():
    assert extract_named_symbols("aaa", r"(x?)a") == []


def test_results_are_deduplicated_in_first_appearance_order():
    assert extract_named_symbols(
        "Can_Init Can_Write Can_Init Can_Write", EXTRACTION.symbol_pattern
    ) == ["Can_Init", "Can_Write"]
