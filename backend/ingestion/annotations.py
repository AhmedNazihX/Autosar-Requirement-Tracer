"""Scan C source for ``@req``/``!req`` requirement annotations (S1.4.3).

The code carries **two** markers with *opposite* meanings (ruling R12):

* ``/* @req 4.0.3/CANIF005 */`` — claims the requirement **is** implemented
* ``/* !req 4.0.3/CANIF316 */`` — claims it is explicitly **not** implemented

Across the in-scope files that is 1454 ``@req`` against 192 ``!req``.
Collapsing the two would make the traceability report claim implementation for
requirements the original authors documented as unimplemented, so the marker is
carried through to :class:`~core.models.ReqAnnotation.claim` and
:attr:`~core.models.CodeUnit.claimed_implemented_ids` filters on it.

The other half of the job is *canonicalization* (ruling R6). The code is
annotated in the AUTOSAR 4.0.3 style (``CANIF023``) while the ingested specs
are R23-11 (``SWS_CANIF_00023``), so a raw annotation joins nothing. The
module prefix is mapped through ``code.annotation_module_map`` and the number
zero-padded into ``code.annotation_id_template``. This normalization is what
gives tier-1 evidence (spec §5) anything to match against; the residual misses
are genuine release drift, which is the product's subject rather than a bug.

Everything pattern-shaped comes from the manifest: no requirement-ID or
annotation regex literal appears in this module.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field

from core.manifest import CodeConfig
from core.models import ReqAnnotation

#: Named groups the manifest's ``annotation_pattern`` must provide.
REQUIRED_GROUPS = ("marker", "module", "num")

#: Optional named group: the AUTOSAR release the annotation cites (``4.0.3/``).
RELEASE_GROUP = "release"


class AnnotationError(Exception):
    """Raised when the *manifest* cannot support annotation scanning.

    Reserved for configuration faults — a pattern missing a required named
    group, a marker with no entry in ``annotation_markers``, an id template
    that does not accept the substitutions. Faults in the *data* (an unknown
    module prefix, say) are warnings instead: the corpus is real code and will
    always contain surprises, and dropping an annotation is the one outcome
    R12 exists to prevent.
    """


@dataclass(frozen=True)
class ScanResult:
    """Annotations found in one blob of source, with non-fatal warnings."""

    annotations: list[ReqAnnotation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def claimed(self) -> list[ReqAnnotation]:
        """Only the ``@req`` annotations (see R12)."""
        return [a for a in self.annotations if a.claim == "claimed_implemented"]

    @property
    def not_claimed(self) -> list[ReqAnnotation]:
        """Only the ``!req`` annotations (see R12)."""
        return [a for a in self.annotations if a.claim == "claimed_not_implemented"]


def line_offsets(text: str) -> list[int]:
    """Character offsets at which each line of ``text`` starts."""
    offsets = [0]
    offsets.extend(i + 1 for i, char in enumerate(text) if char == "\n")
    return offsets


def line_at(offsets: list[int], position: int) -> int:
    """The 1-based line number containing character ``position``."""
    return bisect.bisect_right(offsets, position)


def canonicalize(
    module: str,
    num: str,
    *,
    module_map: dict[str, str],
    id_template: str,
) -> tuple[str, str | None]:
    """Map an annotation's ``module``/``num`` to a canonical requirement id.

    Returns ``(canonical_id, warning_or_None)``. The lookup is
    case-insensitive on the manifest's keys, so ``CANIF``, ``CanIf`` and
    ``canif`` all reach the same entry. An **unmapped** prefix is not an
    error: the raw prefix is used verbatim in the template and a warning is
    returned, so a new module appearing in the code shows up in the extraction
    report instead of crashing ingestion or vanishing from it.
    """
    lookup = {key.upper(): value for key, value in module_map.items()}
    mapped = lookup.get(module.upper())
    warning = None
    if mapped is None:
        mapped = module
        warning = (
            f"module prefix {module!r} is not in code.annotation_module_map "
            f"(known: {', '.join(sorted(module_map))}) — using it verbatim"
        )
    try:
        canonical = id_template.format(module=mapped, num=int(num))
    except (KeyError, IndexError, ValueError) as exc:
        raise AnnotationError(
            f"code.annotation_id_template {id_template!r} cannot be formatted with "
            f"module={mapped!r} num={num!r} — {exc}"
        ) from exc
    return canonical, warning


def scan_text(
    text: str,
    *,
    pattern: str,
    markers: dict[str, str],
    module_map: dict[str, str],
    id_template: str,
    source: str | None = None,
) -> ScanResult:
    """Find every annotation in ``text``, in source order.

    ``source`` (a repo-relative path) is only used to make warnings locatable.

    The scan is a plain regex sweep over the raw text rather than a walk over
    comment tokens: ``@req`` is not valid C outside a comment, and scanning the
    text keeps the scanner usable on a file tree-sitter could not parse at all.
    """
    compiled = re.compile(pattern)
    missing = [g for g in REQUIRED_GROUPS if g not in compiled.groupindex]
    if missing:
        raise AnnotationError(
            f"code.annotation_pattern {pattern!r} is missing required named "
            f"group(s) {missing} — it must capture {list(REQUIRED_GROUPS)} "
            f"(and optionally {RELEASE_GROUP!r})"
        )

    offsets = line_offsets(text)
    annotations: list[ReqAnnotation] = []
    warnings: list[str] = []
    where = f"{source}:" if source else ""

    for match in compiled.finditer(text):
        marker = match.group("marker")
        line = line_at(offsets, match.start())
        if marker not in markers:
            raise AnnotationError(
                f"{where}{line}: marker {marker!r} has no entry in "
                f"code.annotation_markers (known: {sorted(markers)})"
            )
        canonical, warning = canonicalize(
            match.group("module"),
            match.group("num"),
            module_map=module_map,
            id_template=id_template,
        )
        if warning is not None:
            warnings.append(f"{where}{line}: {warning} (raw {match.group(0)!r})")
        try:
            annotations.append(
                ReqAnnotation(
                    canonical_id=canonical,
                    raw=match.group(0),
                    marker=marker,
                    claim=markers[marker],
                    line=line,
                )
            )
        except ValueError as exc:  # pydantic ValidationError subclasses ValueError
            raise AnnotationError(
                f"{where}{line}: cannot build an annotation for {match.group(0)!r} — {exc}"
            ) from exc
    return ScanResult(annotations=annotations, warnings=warnings)


def scan_annotations(text: str, code: CodeConfig, *, source: str | None = None) -> ScanResult:
    """:func:`scan_text` with every pattern taken from a manifest's ``code`` block."""
    return scan_text(
        text,
        pattern=code.annotation_pattern,
        markers=code.annotation_markers,
        module_map=code.annotation_module_map,
        id_template=code.annotation_id_template,
        source=source,
    )
