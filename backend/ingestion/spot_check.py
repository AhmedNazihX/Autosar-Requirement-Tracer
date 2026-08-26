"""``data/spot_check.md`` — the WP1 gate artifact.

Story S1.5.3's acceptance criterion is not "SQLite has N rows"; it is that a
human opens the four PDFs and confirms the extractor did not invent, truncate
or misplace anything. This module picks the requirements to check and renders
them so that check is actually cheap to perform: document **filename** and
**page number** first, then every field a reader can verify against the page.

The sample is **deliberate, not random**. Random sampling of a corpus that is
97% well-behaved mostly re-verifies the easy case. So the selection covers, in
addition to at least one requirement from each of the four documents, the four
shapes where extraction is most likely to be wrong:

* a **titled** requirement — the title is scraped from between the ``[id]`` and
  the ``⌈`` opener, with a character bound, so it is the field most likely to
  have swallowed neighbouring prose or been cut short;
* one **citing upstream requirements** — parsed out of the trailing ``(...)``
  reference list, which is easy to confuse with a sentence in parentheses;
* a **``_CONSTR_``** requirement — a different id shape from the same document,
  which a per-document regex can silently drop;
* one whose **``bbox`` is ``None``** because its body spans a page break. Those
  carry ``char_span`` only, and the split is exactly where a text-extraction
  bug would drop a line, so it is the highest-value case on the list.

Determinism matters: two runs over the same corpus must produce the same
artifact, or a reviewer cannot tell an extraction change from a resampling.
Selection therefore walks documents in manifest order and requirements in
document order, and never consults a random source.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.manifest import DocumentEntry, ProjectManifest
from core.models import Requirement
from ingestion.extraction_report import DocumentIngestion

#: ``(label, predicate)``, in the order they are claimed by a document. The
#: labels appear verbatim in the artifact, so a reviewer can see *why* each
#: requirement is on the list.
CRITERIA: tuple[tuple[str, Callable[[Requirement], bool]], ...] = (
    ("has a title", lambda r: bool(r.title)),
    ("cites upstream requirement ids", lambda r: bool(r.upstream_ids)),
    ("is a `_CONSTR_` constraint", lambda r: "_CONSTR_" in r.id.upper()),
    ("has no bbox — its body spans a page break", lambda r: r.bbox is None),
)

#: Reason given to a pick that exists only to cover its document.
BASELINE_REASON = "baseline coverage for this document"


@dataclass(frozen=True)
class SpotCheckPick:
    """One requirement to verify by hand, and why it was chosen."""

    entry: DocumentEntry
    requirement: Requirement
    #: The criterion this pick was selected for (or :data:`BASELINE_REASON`).
    reason: str
    #: Every criterion the requirement happens to satisfy — context for the
    #: reviewer, and evidence that the sample is not narrower than it looks.
    also_satisfies: tuple[str, ...] = ()


def _satisfied(requirement: Requirement) -> tuple[str, ...]:
    return tuple(label for label, predicate in CRITERIA if predicate(requirement))


def select_picks(results: Sequence[DocumentIngestion]) -> list[SpotCheckPick]:
    """Choose the requirements to verify. Deterministic; see the module docstring.

    Guarantees, given a corpus that contains the shapes at all: at least one
    pick per document, and at least one pick for every criterion in
    :data:`CRITERIA`.
    """
    picks: list[SpotCheckPick] = []
    unmet = [label for label, _ in CRITERIA]
    predicates = dict(CRITERIA)

    # Pass 1: every document contributes one requirement, preferring one that
    # covers a criterion nothing has covered yet.
    for result in results:
        requirements = result.extraction.requirements
        if not requirements:
            continue
        chosen: Requirement | None = None
        reason = BASELINE_REASON
        for label in list(unmet):
            match = next((r for r in requirements if predicates[label](r)), None)
            if match is not None:
                chosen, reason = match, label
                unmet.remove(label)
                break
        if chosen is None:
            chosen = requirements[0]
        picks.append(
            SpotCheckPick(
                entry=result.entry,
                requirement=chosen,
                reason=reason,
                also_satisfies=tuple(c for c in _satisfied(chosen) if c != reason),
            )
        )

    # Pass 2: any criterion still uncovered gets the first requirement in the
    # corpus that satisfies it, wherever it lives.
    already = {(pick.entry.key, pick.requirement.id) for pick in picks}
    for label in list(unmet):
        for result in results:
            match = next(
                (
                    r
                    for r in result.extraction.requirements
                    if predicates[label](r) and (result.entry.key, r.id) not in already
                ),
                None,
            )
            if match is not None:
                picks.append(
                    SpotCheckPick(
                        entry=result.entry,
                        requirement=match,
                        reason=label,
                        also_satisfies=tuple(c for c in _satisfied(match) if c != label),
                    )
                )
                already.add((result.entry.key, match.id))
                unmet.remove(label)
                break
    return picks


def _field(name: str, value: object) -> str:
    return f"- **{name}:** {value}"


def _pick_section(index: int, pick: SpotCheckPick) -> list[str]:
    requirement = pick.requirement
    bbox = (
        "`None` — the body spans a page break, so only `char_span` is available"
        if requirement.bbox is None
        else "`(" + ", ".join(f"{value:.1f}" for value in requirement.bbox) + ")`"
    )
    lines = [
        f"## {index}. `{requirement.id}`",
        "",
        f"### 📄 `{pick.entry.filename}` — **page {requirement.page}**",
        "",
        f"*Selected because it {pick.reason}.*"
        if pick.reason != BASELINE_REASON
        else f"*Selected as {BASELINE_REASON}.*",
        "",
        _field(
            "document",
            f"{pick.entry.title} (`{pick.entry.key}`, module `{pick.entry.module}`)",
        ),
        _field("page", requirement.page),
        _field("id", f"`{requirement.id}`"),
        _field("title", f"{requirement.title!r}" if requirement.title else "*(none)*"),
        _field(
            "section_path",
            f"`{requirement.section_path}`" if requirement.section_path else "*(none)*",
        ),
        _field("bbox", bbox),
        _field(
            "char_span",
            f"`{requirement.char_span}`" if requirement.char_span else "*(none)*",
        ),
        _field(
            "upstream_ids",
            ", ".join(f"`{u}`" for u in requirement.upstream_ids)
            if requirement.upstream_ids
            else "*(none)*",
        ),
        _field(
            "named_symbols",
            ", ".join(f"`{s}`" for s in requirement.named_symbols)
            if requirement.named_symbols
            else "*(none)*",
        ),
        _field("doc_type", f"`{requirement.doc_type}`"),
    ]
    if pick.also_satisfies:
        lines.append(_field("also covers", "; ".join(pick.also_satisfies)))
    lines.extend(
        [
            "",
            "**text**",
            "",
            "> " + requirement.text.replace("\n", "\n> "),
            "",
        ]
    )
    return lines


def render_spot_check(
    manifest: ProjectManifest, picks: Sequence[SpotCheckPick]
) -> str:
    """Render the artifact as Markdown."""
    covered = {pick.reason for pick in picks} | {
        label for pick in picks for label in pick.also_satisfies
    }
    lines = [
        "# Spot check — WP1 gate",
        "",
        f"- project: `{manifest.project_id}` ({manifest.name}), corpus version "
        f"`{manifest.version}`",
        f"- generated: {datetime.now(UTC).isoformat(timespec='seconds')}",
        f"- requirements to verify: **{len(picks)}** across {len({p.entry.key for p in picks})} "
        f"of {len(manifest.documents)} document(s)",
        "",
        "**How to verify.** Open the PDF named under each heading in "
        "`data/docs/`, go to the page number given, and confirm that the id, "
        "the title, the body text, the upstream references and the section all "
        "match what is printed there. `page` is 1-based and is the page *of the "
        "PDF*, which for these documents equals the page number printed in the "
        "footer.",
        "",
        "The sample is deliberate rather than random — see "
        "`backend/ingestion/spot_check.py` for why these shapes. Coverage:",
        "",
    ]
    for label, _ in CRITERIA:
        mark = "x" if label in covered else " "
        lines.append(f"- [{mark}] {label}")
    lines.extend(
        [
            "",
            "| # | id | document | page | selected because |",
            "|---|---|---|---|---|",
        ]
    )
    for index, pick in enumerate(picks, start=1):
        lines.append(
            f"| {index} | `{pick.requirement.id}` | `{pick.entry.filename}` "
            f"| {pick.requirement.page} | {pick.reason} |"
        )
    lines.append("")
    for index, pick in enumerate(picks, start=1):
        lines.extend(_pick_section(index, pick))
    return "\n".join(lines).rstrip() + "\n"


def write_spot_check(
    manifest: ProjectManifest,
    results: Sequence[DocumentIngestion],
    path: Path,
) -> tuple[Path, list[SpotCheckPick]]:
    """Select, render and write the spot check; return the path and the picks."""
    picks = select_picks(results)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_spot_check(manifest, picks), encoding="utf-8")
    return path, picks
