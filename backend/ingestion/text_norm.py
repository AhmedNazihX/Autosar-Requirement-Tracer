"""The requirement-text normalization contract. **This module is the spec.**

Ratified as binding on the requirement extractor (story S1.3.3): the golden
fixtures in ``backend/tests/fixtures/golden/`` were hand-labeled under
exactly these rules, so the extractor must call
:func:`normalize_requirement_text` rather than reimplement it, or every
fixture fails. ``fixtures/golden/README.md`` points here instead of
restating the rules; there is deliberately only one copy.

Why normalization is needed at all: the AUTOSAR PDFs are LaTeX-typeset and
justified, so PyMuPDF's text has hard line breaks at arbitrary points —
including *inside* identifiers (``Can_-\\nSetControllerMode``) and inside
upstream requirement ids (``SRS_BSW_-\\n00369``) — plus a running header and
footer on every page. Requirement text is unusable, and upstream ids are
unmatchable, until that is undone.

This module knows nothing about requirements: no ``SWS_`` pattern, no
``⌈``/``⌋``. It takes the patterns it applies from the manifest's
``extraction`` section, which is what keeps the corpus swappable (spec §2).

The contract, applied in this order:

1. **Page furniture** — every regex in ``extraction.footer_patterns`` is
   replaced with a **single newline** (``"\\n"``), *not* deleted. The
   substitution is load-bearing: the patterns are anchored on the newlines
   around the furniture, and the first pattern
   (``\\n<N> of <M>\\nDocument ID <k>: <doc>\\n``) consumes the newline that
   the second (``\\n<title>\\nAUTOSAR CP R23-11\\n``) needs in order to match
   the immediately following page header. Deleting instead of substituting
   leaves the next page's header in the text and fuses words across the page
   break (``driver.Specification``).
2. **Table-continuation markers** — every string in
   ``extraction.drop_markers`` (``▽``, ``△``) is replaced with a **single
   space**, not deleted, so a marker sitting between two words cannot fuse
   them. Step 4 collapses the space away wherever it was not needed.
3. **Hyphenation** — every ``-\\n`` is deleted, rejoining a word split across
   a line break (``partici-\\npating`` → ``participating``,
   ``Can_-\\nSetControllerMode`` → ``Can_SetControllerMode``,
   ``SRS_BSW_-\\n00369`` → ``SRS_BSW_00369``). This is lossy in principle: a
   genuine end-of-line hyphen in a compound word is swallowed. No such case
   occurs on the seven fixture pages (each was checked by hand); corpus-wide
   it will occasionally be wrong, which is an accepted trade for making
   identifiers and upstream ids recoverable at all.
4. **Whitespace** — every remaining run of whitespace collapses to one space,
   then the result is stripped.

Nothing else is removed. Bullet markers (``•``), the source's curly
apostrophes (``’``) and hyphens that were not at a line break all survive
verbatim, as do the source's own spelling mistakes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

#: What a ``footer_patterns`` match is replaced with. See rule 1 — this is a
#: newline rather than an empty string, and the fixtures depend on it.
FURNITURE_REPLACEMENT = "\n"

#: What a ``drop_markers`` occurrence is replaced with. See rule 2.
MARKER_REPLACEMENT = " "

#: The document-title placeholder inside a ``footer_patterns`` entry.
TITLE_PLACEHOLDER = "{title}"


class NormalizationError(ValueError):
    """Raised when normalization is asked to do something meaningless.

    Subclasses :class:`ValueError` so a caller that only guards against bad
    arguments in general still catches it.
    """


def expand_footer_pattern(pattern: str, document_title: str) -> str:
    """Substitute ``document_title`` into ``pattern``'s ``{title}`` placeholder.

    The title is ``re.escape``-d, so a title containing regex metacharacters
    is matched literally.

    A pattern that *needs* a title but is handed an empty or whitespace-only
    one is an error, not a degradation: silently expanding
    ``'\\n{title}\\nAUTOSAR CP R23-11\\n'`` to
    ``'\\n\\nAUTOSAR CP R23-11\\n'`` still matches on some pages, so the
    caller would get a partially-stripped document and no signal at all.
    """
    if TITLE_PLACEHOLDER not in pattern:
        return pattern
    if not document_title.strip():
        raise NormalizationError(
            f"footer pattern {pattern!r} contains {TITLE_PLACEHOLDER} but document_title is "
            f"{document_title!r} — pass the owning document's title from the manifest "
            "(documents[].title)"
        )
    return pattern.replace(TITLE_PLACEHOLDER, re.escape(document_title))


def strip_page_furniture(
    text: str, footer_patterns: Iterable[str], document_title: str
) -> str:
    """Rule 1: replace each running header/footer match with a newline.

    ``document_title`` is substituted into the ``{title}`` placeholder and
    regex-escaped, so a title containing regex metacharacters is matched
    literally. Raises :class:`NormalizationError` if a pattern needs a title
    and none was given.
    """
    for pattern in footer_patterns:
        expanded = expand_footer_pattern(pattern, document_title)
        text = re.sub(expanded, FURNITURE_REPLACEMENT, text)
    return text


def replace_markers(text: str, markers: Iterable[str]) -> str:
    """Rule 2: replace each table-continuation marker with a space."""
    for marker in markers:
        text = text.replace(marker, MARKER_REPLACEMENT)
    return text


def rejoin_hyphenated(text: str) -> str:
    """Rule 3: delete every ``-\\n``, rejoining a word split across a line."""
    return text.replace("-\n", "")


def collapse_whitespace(text: str) -> str:
    """Rule 4: collapse whitespace runs to one space, then strip."""
    return re.sub(r"\s+", " ", text).strip()


def normalize_requirement_text(
    text: str,
    *,
    footer_patterns: Sequence[str] = (),
    drop_markers: Sequence[str] = (),
    document_title: str = "",
) -> str:
    """Apply the full contract (rules 1–4) to ``text``.

    Pass ``manifest.extraction.footer_patterns`` and
    ``manifest.extraction.drop_markers`` together with the owning document's
    ``title``. Rule 1 is a no-op when ``footer_patterns`` is empty, which is
    the right behaviour for a span known to contain no page break — but a
    non-empty ``footer_patterns`` containing ``{title}`` with no
    ``document_title`` raises :class:`NormalizationError`.
    """
    text = strip_page_furniture(text, footer_patterns, document_title)
    text = replace_markers(text, drop_markers)
    text = rejoin_hyphenated(text)
    return collapse_whitespace(text)


def extract_named_symbols(text: str, symbol_pattern: str) -> list[str]:
    """Every ``symbol_pattern`` match, first-appearance order, deduplicated.

    Run against text that has already been through
    :func:`normalize_requirement_text` — a symbol split across a line break
    is not matchable before rule 3 has rejoined it. This is the rule the
    fixtures' ``named_symbols`` lists were labeled under, and
    ``test_golden_fixtures.py`` asserts the labels equal this function's
    output exactly, so an omission fails the suite.

    The symbol taken from a match is the first *participating* capturing
    group, falling back to the whole match when the pattern has no groups (or
    none of them participated). Indexing ``group(1)`` unconditionally would
    return ``None`` for a pattern whose first group is optional and did not
    take part, and ``None`` would then land in the returned list.
    """
    compiled = re.compile(symbol_pattern)
    seen: list[str] = []
    for match in compiled.finditer(text):
        symbol = next((group for group in match.groups() if group is not None), match.group(0))
        if symbol and symbol not in seen:
            seen.append(symbol)
    return seen
