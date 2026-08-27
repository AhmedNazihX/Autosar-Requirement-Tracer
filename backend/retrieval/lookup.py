"""Exact requirement lookup by ID (story S2.1.1, tool ``lookup_requirement``).

Spec §3 is emphatic that this path is **never semantic**: a user who names a
requirement gets that requirement or a clean miss. Returning a plausible
neighbour would be worse than returning nothing, because a traceability answer
citing the wrong requirement is indistinguishable from a right one.

The problem this module solves is that the id a person types is rarely the id
the document stores. ``sws_can_11``, ``SWS_Can_00011`` and ``CANIF-23`` are all
things people actually write, and finding B3 makes it worse: the CAN Driver
document spells 205 of its requirements ``SWS_Can_*`` and 7 of them
``SWS_CAN_*``, while the other three documents each use their own casing. So a
lookup must be forgiving about spelling and absolutely exact about identity.

**Why a fingerprint rather than a canonicalisation rule.** The obvious
approach — rebuild the "correct" id from the input (upper-case it, zero-pad the
number to five digits) — needs two pieces of corpus knowledge that CLAUDE.md
puts in the manifest and not in code: the id grammar and the padding width. It
also breaks on this very corpus, where ``SWS_Can_91025`` is five digits with no
padding at all while ``SWS_Can_00011`` is five digits of which three are
padding.

So instead both sides are reduced to a :func:`fingerprint` — letters and digits
only, an ``SWS`` prefix dropped, leading zeros inside each number dropped — and
those are compared. It needs no grammar, survives a corpus swap, and was
checked against the real index: **zero collisions across all 1054
requirements** (two spellings of one id collapse together, which is the point;
two different ids never do).

Lookup tries the indexed exact match first and only falls back to the
fingerprint scan when that misses, so the common case stays an index hit.

**A miss and a bad request are different things.** An id that cannot name any
requirement (no digits, no letters, longer than :data:`MAX_ID_LENGTH`) raises
:class:`InvalidRequirementId` — the caller's input is wrong, and WP3 maps that
to a 400. A well-formed id that simply is not in the corpus returns ``None``,
which is a 404. Collapsing the two would tell a user their typo does not exist.
"""

from __future__ import annotations

import re
import sqlite3

from core import db
from core.models import Requirement

#: Longest id string accepted, before normalisation. Real ids are ~22
#: characters; this is loose enough for any corpus and tight enough that story
#: S6.3.1's length cap is enforced here rather than only at the HTTP edge.
MAX_ID_LENGTH = 64

#: Everything that is not a letter or a digit is a separator: ``_``, spaces,
#: ``-``, ``.`` and anything else a person might paste.
_NOISE = re.compile(r"[^0-9A-Za-z]+")

#: Leading zeros inside a run of digits. ``00011`` and ``11`` are the same
#: number, and this corpus writes both (see the module docstring).
_LEADING_ZEROS = re.compile(r"0*(\d+)")

#: Dropped from the front of a fingerprint so ``SWS_Can_00011`` and ``Can_11``
#: agree. It is the only structural assumption here, it is what the standard
#: family this project targets uses, and a corpus without it is unaffected —
#: the prefix simply never appears to be stripped.
_DOCUMENT_PREFIX = "SWS"


class InvalidRequirementId(ValueError):
    """The given string cannot name a requirement at all.

    Distinct from "no such requirement": this is a malformed request, and
    story S3.3.2 turns it into a 400 rather than a 404.
    """


def fingerprint(raw: str) -> str:
    """Reduce an id to what is identifying about it. A pure function.

    ``SWS_Can_00011``, ``sws_can_11``, ``can 11`` and ``Can-11`` all become
    ``CAN11``. Case, separators, the document prefix and zero-padding are all
    ways of writing the same id; the module letters and the number are the id.

    Returns ``""`` for a string with nothing identifying in it, which
    :func:`lookup` reports as :class:`InvalidRequirementId`.
    """
    compact = _NOISE.sub("", raw).upper()
    if compact.startswith(_DOCUMENT_PREFIX):
        compact = compact[len(_DOCUMENT_PREFIX) :]
    return _LEADING_ZEROS.sub(lambda match: match.group(1), compact)


def _validated_fingerprint(raw: str) -> str:
    """:func:`fingerprint`, with the reasons it cannot work made explicit."""
    if len(raw) > MAX_ID_LENGTH:
        raise InvalidRequirementId(
            f"requirement id is {len(raw)} characters, longer than the {MAX_ID_LENGTH} "
            "character limit"
        )
    probe = fingerprint(raw)
    if not probe:
        raise InvalidRequirementId(f"{raw!r} contains nothing that could be a requirement id")
    if not any(character.isdigit() for character in probe):
        raise InvalidRequirementId(f"{raw!r} has no requirement number")
    if not any(character.isalpha() for character in probe):
        raise InvalidRequirementId(f"{raw!r} has no module name, so the number is ambiguous")
    return probe


#: Deliberately narrow: the scan compares fingerprints of ids and needs
#: nothing else, so the row it reads is one column wide. The full record is
#: fetched through the indexed lookup once a match is known.
_CANDIDATE_IDS_SQL = """
SELECT id FROM requirements
WHERE project_id = ? AND doc_type = 'requirement'
ORDER BY id
"""


def lookup(conn: sqlite3.Connection, project_id: str, raw_id: str) -> Requirement | None:
    """The requirement ``raw_id`` names, or ``None`` if there is no such one.

    Raises :class:`InvalidRequirementId` when ``raw_id`` could not name any
    requirement. Context chunks are never returned: they are prose with a
    synthetic id, and nothing should cite one.

    What comes back is the *stored* record, never the caller's spelling:
    citations render its id verbatim and finding B3 means the two often
    differ. It carries everything a citation needs — ``source_doc`` is the
    manifest key (never a filename), and ``bbox`` is ``None`` for the ~6% of
    requirements whose text crosses a page break, so the pane opens the right
    page without a highlight instead of highlighting the wrong paragraph.
    """
    probe = _validated_fingerprint(raw_id)

    # Fast path: the id was already written the way it is stored. Uses the
    # canonical_id index, and covers the overwhelmingly common case of an id
    # copied out of a document or replayed from a stored citation.
    exact = db.get_requirement(conn, project_id, raw_id)
    if exact is not None and exact.doc_type == "requirement":
        return exact

    # Fallback: compare fingerprints. A scan, but of one project's
    # requirements only (1054 in this corpus) and only when the fast path
    # missed, which is cheap enough that a second index would be a liability
    # to keep in sync for no measurable gain.
    # ``ORDER BY id`` makes the outcome deterministic even if a future corpus
    # did contain two ids sharing a fingerprint (this one contains none).
    for row in conn.execute(_CANDIDATE_IDS_SQL, (project_id,)):
        if fingerprint(row["id"]) == probe:
            return db.get_requirement(conn, project_id, row["id"])
    return None
