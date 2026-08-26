"""Normalization that remembers where every character came from.

The extractor has two irreconcilable needs:

* the requirement regex only works on **cleaned** text — page furniture
  removed, words rejoined across line breaks (``ingestion.text_norm``);
* ``Requirement.char_span`` and ``Requirement.bbox`` must index the **raw**
  document that ``ingestion.pdf_loader`` produced, because that is what the
  PDF viewer resolves against.

Cleaning deletes characters, so the two coordinate systems diverge. This
module applies the cleaning rules while carrying an origin index alongside
the text, so a match found in cleaned coordinates can be reported in raw
document coordinates.

**``ingestion.text_norm`` remains the single source of truth for *what*
cleaning does.** This module only changes *how* it is applied: it reuses that
module's own patterns and replacement constants, and
``tests/test_mapped_text.py`` asserts that
``collapse_whitespace(clean_with_offsets(t).text)`` is byte-identical to
``normalize_requirement_text(t)``. If the two ever disagree, that test fails
rather than the extractor silently drifting from the golden fixtures.

Only rules 1–3 are applied here. Rule 4 (whitespace collapse) is deliberately
left out: it would destroy the line structure that section-heading detection
and paragraph splitting rely on, and the requirement regex does not need it
(it runs with ``re.S``). Rule 4 is applied per-span, by calling
:func:`~ingestion.text_norm.normalize_requirement_text` on the raw text a
match covers — which re-runs rules 1–3 as idempotent no-ops and then
collapses whitespace, yielding exactly the fixture-labeled text.
"""

from __future__ import annotations

import re
from array import array
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass

from ingestion.text_norm import (
    FURNITURE_REPLACEMENT,
    MARKER_REPLACEMENT,
    expand_footer_pattern,
)

#: Rule 3's target: a hyphen immediately before a line break, deleted whole.
HYPHEN_BREAK = "-\n"

#: ``array`` type code for the origin index. ``"l"`` is at least 32 bits, so
#: it comfortably covers documents of a few hundred thousand characters.
_ORIGIN_TYPECODE = "l"


@dataclass(frozen=True)
class MappedText:
    """Cleaned text plus, per character, the raw offset it came from.

    ``origins[i]`` is the offset in :attr:`source_length`-long raw text that
    ``text[i]`` originated at. The sequence is non-decreasing — cleaning only
    deletes characters or replaces a run in place — which is what makes
    :meth:`cleaned_index` a plain binary search.
    """

    text: str
    origins: array
    source_length: int

    def __post_init__(self) -> None:
        if len(self.origins) != len(self.text):
            raise ValueError(
                f"origin index has {len(self.origins)} entries for {len(self.text)} characters"
            )

    def source_offset(self, index: int) -> int:
        """Raw offset that cleaned character ``index`` came from."""
        if not 0 <= index <= len(self.text):
            raise IndexError(f"cleaned index {index} out of range (0..{len(self.text)})")
        if index == len(self.text):
            return self.source_length
        return self.origins[index]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """Raw ``[start, end)`` span covered by cleaned ``[start, end)``.

        The end is taken from the *last* character of the span rather than
        from ``end`` itself, so trailing deleted characters (a stripped footer
        sitting immediately after a match) are not pulled into the span.
        """
        if end < start:
            raise ValueError(f"span end {end} precedes start {start}")
        if end == start:
            offset = self.source_offset(start)
            return offset, offset
        return self.source_offset(start), self.source_offset(end - 1) + 1

    def cleaned_index(self, source_offset: int) -> int:
        """First cleaned index whose origin is at or after ``source_offset``.

        The inverse of :meth:`source_offset`, up to deleted characters: a raw
        offset that cleaning removed maps to the cleaned position where the
        removed run used to start.
        """
        return bisect_left(self.origins, source_offset)


def _sub_mapped(mapped: MappedText, pattern: re.Pattern[str], replacement: str) -> MappedText:
    """Replace every ``pattern`` match with ``replacement``, keeping origins.

    Every character of ``replacement`` is attributed to the first character of
    the run it replaced. That is exactly right for the rules this module
    applies: rule 1's ``"\\n"`` replacement *is* the newline the footer
    pattern's own match starts with, and rule 2 replaces a single marker
    character in place.
    """
    matches = [m for m in pattern.finditer(mapped.text) if m.end() > m.start()]
    if not matches:
        return mapped

    chunks: list[str] = []
    origins = array(_ORIGIN_TYPECODE)
    position = 0
    for match in matches:
        start, end = match.span()
        chunks.append(mapped.text[position:start])
        origins.extend(mapped.origins[position:start])
        if replacement:
            chunks.append(replacement)
            origins.extend([mapped.origins[start]] * len(replacement))
        position = end
    chunks.append(mapped.text[position:])
    origins.extend(mapped.origins[position:])
    return MappedText("".join(chunks), origins, mapped.source_length)


def clean_with_offsets(
    text: str,
    *,
    footer_patterns: Sequence[str] = (),
    drop_markers: Sequence[str] = (),
    document_title: str = "",
) -> MappedText:
    """Apply normalization rules 1–3 to ``text``, tracking origins.

    Argument names and order mirror
    :func:`~ingestion.text_norm.normalize_requirement_text` so the two are
    obviously the same operation minus rule 4.
    """
    mapped = MappedText(text, array(_ORIGIN_TYPECODE, range(len(text))), len(text))

    for pattern in footer_patterns:  # rule 1
        expanded = expand_footer_pattern(pattern, document_title)
        mapped = _sub_mapped(mapped, re.compile(expanded), FURNITURE_REPLACEMENT)
    for marker in drop_markers:  # rule 2
        mapped = _sub_mapped(mapped, re.compile(re.escape(marker)), MARKER_REPLACEMENT)
    return _sub_mapped(mapped, re.compile(re.escape(HYPHEN_BREAK)), "")  # rule 3
