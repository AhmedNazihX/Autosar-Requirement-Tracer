"""Tests for the requirement extractor (story S1.3.3).

The headline test is :func:`test_golden_recall_per_fixture_page` — the story's
acceptance criterion. It runs the extractor over the committed
``*.blocks.json`` parser output and compares the result, field by field,
against the hand-labeled ``*.expected.json``. Recall must be **≥ 90%**; the
measured figure is printed for every page and for the set as a whole.

Nothing here needs ``data/``: the fixtures carry the frozen parser output, so
the suite passes on a fresh clone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.manifest import load_manifest
from core.models import Requirement
from ingestion.req_extractor import (
    build_block_pattern,
    extract_requirements,
    find_headings,
    heading_for_offset,
    locate_span,
    merge_spans,
)
from tests.support_extraction import (
    document_from_blocks,
    document_from_golden,
    following_fixture,
    golden_index,
    load_golden,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
DOCUMENTS = {doc.key: doc for doc in MANIFEST.documents}
INDEX = golden_index()
FIXTURE_IDS = [entry["fixture"] for entry in INDEX]

#: The story's acceptance bar. Below this, the plan opens an LLM-fallback
#: story — which is a controller decision, so this constant never moves to
#: make a run pass.
RECALL_THRESHOLD = 0.90


def extract_for_fixture(fixture: str) -> tuple[list[Requirement], dict[str, Any]]:
    """Run the extractor over ``fixture`` (plus its continuation page).

    Returns only the requirements the extractor attributes to *this* fixture's
    page, which is also the rule the labels follow: a requirement is recorded
    on the page its id appears on, never on the page its body happens to end.
    """
    entry = next(e for e in INDEX if e["fixture"] == fixture)
    document_entry = DOCUMENTS[entry["document_key"]]

    fixtures = [fixture]
    successor = following_fixture(entry, INDEX)
    if successor is not None:
        fixtures.append(successor)

    document = document_from_golden(*fixtures)
    extraction = extract_requirements(document, MANIFEST, document_entry)
    on_page = [req for req in extraction.requirements if req.page == entry["page"]]
    return on_page, load_golden(fixture, "expected")


def field_mismatches(actual: Requirement, expected: dict[str, Any]) -> list[str]:
    """Every field of ``expected`` that ``actual`` does not reproduce exactly."""
    problems = []
    comparisons = (
        ("text", actual.text, expected["text"]),
        ("title", actual.title, expected["title"]),
        ("upstream_ids", actual.upstream_ids, expected["upstream_ids"]),
        ("named_symbols", actual.named_symbols, expected["named_symbols"]),
    )
    for name, got, want in comparisons:
        if got != want:
            problems.append(f"{expected['id']}.{name}: extracted {got!r} but label says {want!r}")
    return problems


# --------------------------------------------------------------------------
# the acceptance criterion
# --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", FIXTURE_IDS)
def test_golden_recall_per_fixture_page(fixture: str, capsys: pytest.CaptureFixture[str]):
    """Every labeled requirement on the page must be extracted, exactly."""
    extracted, expected = extract_for_fixture(fixture)
    labels = expected["requirements"]
    by_id = {req.id: req for req in extracted}

    missing = [label["id"] for label in labels if label["id"] not in by_id]
    problems: list[str] = []
    for label in labels:
        actual = by_id.get(label["id"])
        if actual is not None:
            problems.extend(field_mismatches(actual, label))

    found = len(labels) - len(missing)
    exact = sum(
        1
        for label in labels
        if label["id"] in by_id and not field_mismatches(by_id[label["id"]], label)
    )
    with capsys.disabled():
        print(
            f"\n  recall[{fixture}]: id {found}/{len(labels)}, exact {exact}/{len(labels)}"
            + (f" (extra extracted: {sorted(set(by_id) - {q['id'] for q in labels})})" if
               set(by_id) - {q["id"] for q in labels} else "")
        )

    assert not missing, f"{fixture}: labeled requirements not extracted: {missing}"
    assert not problems, f"{fixture}: field mismatches:\n" + "\n".join(problems)
    assert set(by_id) == {label["id"] for label in labels}, (
        f"{fixture}: extractor produced requirements the page does not define: "
        f"{sorted(set(by_id) - {label['id'] for label in labels})}"
    )


def test_golden_recall_over_the_whole_fixture_set(capsys: pytest.CaptureFixture[str]):
    """The single number the story is graded on."""
    labeled = 0
    recalled = 0
    exact = 0
    misses: list[str] = []

    for fixture in FIXTURE_IDS:
        extracted, expected = extract_for_fixture(fixture)
        by_id = {req.id: req for req in extracted}
        for label in expected["requirements"]:
            labeled += 1
            actual = by_id.get(label["id"])
            if actual is None:
                misses.append(f"{fixture}:{label['id']}")
                continue
            recalled += 1
            if not field_mismatches(actual, label):
                exact += 1

    recall = recalled / labeled
    exact_recall = exact / labeled
    with capsys.disabled():
        print(
            f"\n  GOLDEN RECALL: {recalled}/{labeled} ids = {recall:.4f}; "
            f"exact on all four fields {exact}/{labeled} = {exact_recall:.4f}"
        )

    assert labeled >= 30, "the fixture set is too small to measure recall against"
    assert recall >= RECALL_THRESHOLD, f"id recall {recall:.4f} below {RECALL_THRESHOLD}: {misses}"
    assert exact_recall >= RECALL_THRESHOLD, f"exact recall {exact_recall:.4f} below threshold"


# --------------------------------------------------------------------------
# the specific cases each fixture was frozen for
# --------------------------------------------------------------------------


def test_the_tracing_table_page_yields_zero_requirements():
    """§6 lists ids that satisfy SRS items; none of them is a definition."""
    extracted, expected = extract_for_fixture("can_driver_p023")
    assert expected["requirements"] == []
    assert extracted == [], (
        "the Requirements-Tracing table produced requirements — the ⌈…⌋ requirement "
        f"in the block pattern is not holding: {[r.id for r in extracted]}"
    )


def test_the_titled_variant_splits_the_title_out_of_the_text():
    extracted, _ = extract_for_fixture("can_driver_p043")
    by_id = {req.id: req for req in extracted}
    titled = by_id["SWS_Can_91019"]

    assert titled.title == "Definiton of development errors in module Can"
    assert titled.title not in titled.text, "the title must not be duplicated inside the body"
    assert titled.text.startswith("Type of error")
    assert by_id["SWS_Can_00026"].title is None, "an untitled requirement must have title=None"


def test_the_page_break_requirement_has_no_bbox_but_a_real_char_span():
    extracted, _ = extract_for_fixture("can_driver_p034")
    spanning = next(req for req in extracted if req.id == "SWS_Can_00407")

    assert spanning.bbox is None, "a requirement crossing a page break has no honest bbox"
    assert spanning.char_span is not None
    start, end = spanning.char_span
    assert end > start

    document = document_from_golden("can_driver_p034", "can_driver_p035")
    assert document.pages_for_span(start, end) == [34, 35]
    assert "SWS_Can_00407" in document.text[start:end]

    # Every requirement that does *not* cross a page break must have a bbox.
    for req in extracted:
        if req.id != "SWS_Can_00407":
            assert req.bbox is not None, f"{req.id}: bbox unexpectedly unresolved"


def test_a_requirements_bbox_is_the_union_of_the_blocks_it_covers():
    """The titled variant's body is a table spread over many blocks."""
    extracted, _ = extract_for_fixture("can_driver_p043")
    titled = next(req for req in extracted if req.id == "SWS_Can_91019")
    document = document_from_golden("can_driver_p043")
    start, end = titled.char_span

    assert titled.bbox is not None
    covering = [
        block
        for block in document.page(43).blocks
        if block.char_start < end and block.char_end > start
    ]
    assert len(covering) > 2, "this body really does span several blocks"
    assert titled.bbox == (
        min(block.bbox[0] for block in covering),
        min(block.bbox[1] for block in covering),
        max(block.bbox[2] for block in covering),
        max(block.bbox[3] for block in covering),
    )


def test_the_constr_infix_is_extracted():
    extracted, _ = extract_for_fixture("can_driver_p094")
    ids = sorted(req.id for req in extracted)
    assert ids == [
        "SWS_Can_CONSTR_00509",
        "SWS_Can_CONSTR_00510",
        "SWS_Can_CONSTR_00511",
    ]


def test_both_id_casings_survive_extraction():
    driver, _ = extract_for_fixture("can_driver_p032")
    interface, _ = extract_for_fixture("can_interface_p034")
    assert all(req.id.startswith("SWS_Can_") for req in driver)
    assert all(req.id.startswith("SWS_CANIF_") for req in interface)


def test_char_span_is_always_set_and_points_at_the_requirement():
    for fixture in FIXTURE_IDS:
        extracted, _ = extract_for_fixture(fixture)
        entry = next(e for e in INDEX if e["fixture"] == fixture)
        successor = following_fixture(entry, INDEX)
        document = document_from_golden(
            *( [fixture] if successor is None else [fixture, successor])
        )
        for req in extracted:
            assert req.char_span is not None, f"{req.id}: char_span must never be None"
            start, end = req.char_span
            assert req.id in document.text[start:end], (
                f"{req.id}: char_span {req.char_span} does not cover its own id"
            )


def test_section_path_is_the_heading_in_force():
    """The heading on page 43 is '7.12.1 Development Errors'."""
    extracted, _ = extract_for_fixture("can_driver_p043")
    paths = {req.section_path for req in extracted}
    assert "7.12.1 Development Errors" in paths


# --------------------------------------------------------------------------
# the rules no frozen page happens to exercise
# --------------------------------------------------------------------------


def can_driver_document():
    return DOCUMENTS["can_driver"]


def test_dehyphenation_rejoins_words_split_across_a_line_break():
    document = document_from_blocks(
        [
            "[SWS_Can_00001] ⌈The Can module shall raise an RX indica-\n"
            "tion for every multi-\nplexed frame.⌋(SRS_Can_00001)\n"
        ]
    )
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert len(extraction.requirements) == 1
    text = extraction.requirements[0].text
    assert "indication" in text and "multiplexed" in text
    assert "indica- tion" not in text and "indica-\ntion" not in text


def test_upstream_ids_accept_the_rs_prefix_not_only_srs():
    """RS_Ids_00810 (Intrusion Detection System) really appears in the corpus."""
    document = document_from_blocks(
        [
            "[SWS_CANIF_01234] ⌈CanIf shall report the security event.⌋"
            "(RS_Ids_00810, SRS_Can_01059)\n"
        ]
    )
    extraction = extract_requirements(document, MANIFEST, DOCUMENTS["can_interface"])
    assert extraction.requirements[0].upstream_ids == ["RS_Ids_00810", "SRS_Can_01059"]


def test_upstream_ids_survive_a_line_break_inside_the_id():
    document = document_from_blocks(
        ["[SWS_Can_00089] ⌈Errors shall be reported.⌋(SRS_BSW_-\n00369, SRS_BSW_00386)\n"]
    )
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert extraction.requirements[0].upstream_ids == ["SRS_BSW_00369", "SRS_BSW_00386"]


def test_upstream_ids_are_deduplicated_in_first_appearance_order():
    document = document_from_blocks(
        ["[SWS_Can_00002] ⌈Text.⌋(SRS_Can_01060, SRS_BSW_00101, SRS_Can_01060)\n"]
    )
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert extraction.requirements[0].upstream_ids == ["SRS_Can_01060", "SRS_BSW_00101"]


def test_ids_referenced_inside_a_body_are_not_upstream_ids():
    document = document_from_blocks(
        ["[SWS_Can_00003] ⌈See SRS_Can_09999 for background.⌋(SRS_Can_01060)\n"]
    )
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert extraction.requirements[0].upstream_ids == ["SRS_Can_01060"]


def test_a_bracketed_reference_does_not_start_a_match():
    """The title group forbids brackets; that is what kills false positives."""
    document = document_from_blocks(
        [
            "Compare to [SWS_Can_00200] and [SWS_Can_00398] for the state machine.\n",
            "[SWS_Can_00409] ⌈When the function is entered it shall detect it.⌋()\n",
        ]
    )
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert [req.id for req in extraction.requirements] == ["SWS_Can_00409"]


def test_prose_after_the_closing_floor_is_not_part_of_the_text():
    document = document_from_blocks(
        [
            "[SWS_Can_00420] ⌈The Can module shall reset the interrupt flag.⌋() "
            "Implementation hint: the vector table is out of scope.\n"
        ]
    )
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert extraction.requirements[0].text == "The Can module shall reset the interrupt flag."


def test_patterns_come_from_the_manifest_not_from_python():
    """A req_id_pattern that matches nothing must yield nothing."""
    document = document_from_golden("can_driver_p032")
    neutered = MANIFEST.model_copy(deep=True)
    neutered.extraction.req_id_pattern = r"NOTHING_MATCHES_THIS_\d+"

    assert extract_requirements(document, neutered, can_driver_document()).requirements == []
    # ... while the real manifest finds all nine on the same page.
    assert len(extract_requirements(document, MANIFEST, can_driver_document()).requirements) == 9


def test_a_neutered_body_delimiter_also_yields_nothing():
    document = document_from_golden("can_driver_p032")
    neutered = MANIFEST.model_copy(deep=True)
    neutered.extraction.body_open = "@@"
    neutered.extraction.body_close = "@@"
    assert extract_requirements(document, neutered, can_driver_document()).requirements == []


def test_extraction_warns_but_does_not_fail_on_a_count_deviation():
    document = document_from_golden("can_driver_p032")
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert len(extraction.requirements) == 9
    assert extraction.warnings, "a 9-vs-240 deviation must be warned about"
    assert "240" in extraction.warnings[0]


def test_misses_record_unmatched_body_openers_with_context():
    """An id that fails req_id_pattern is reported, never silently dropped."""
    document = document_from_blocks(
        [
            "[SWS_Can_00001] ⌈A real requirement.⌋()\n",
            "11 Not applicable requirements\n",
            "[SWS_Can_NA] ⌈These requirements are not applicable to this "
            "specification.⌋(SRS_BSW_00162)\n",
        ]
    )
    extraction = extract_requirements(document, MANIFEST, can_driver_document())
    assert [req.id for req in extraction.requirements] == ["SWS_Can_00001"]
    assert len(extraction.misses) == 1
    miss = extraction.misses[0]
    assert miss.nearest_bracketed == "SWS_Can_NA"
    assert "not applicable" in miss.context
    assert miss.page == 1
    assert extraction.body_opener_count == 2


def test_every_extracted_requirement_carries_the_manifest_provenance():
    extracted, _ = extract_for_fixture("can_driver_p032")
    for req in extracted:
        assert req.project_id == MANIFEST.project_id
        assert req.version == MANIFEST.version
        # The manifest KEY, not the filename: one document identity end to
        # end, from storage through the API path to a citation event.
        assert req.source_doc == DOCUMENTS["can_driver"].key == "can_driver"
        assert req.source_doc != DOCUMENTS["can_driver"].filename
        assert req.doc_type == "requirement"


# --------------------------------------------------------------------------
# the small pure helpers
# --------------------------------------------------------------------------


def test_build_block_pattern_uses_the_manifest_delimiters():
    pattern = build_block_pattern(MANIFEST)
    assert MANIFEST.extraction.body_open in pattern.pattern
    assert MANIFEST.extraction.req_id_pattern in pattern.pattern


def test_find_headings_detects_a_two_line_block_and_ignores_everything_else():
    document = document_from_blocks(
        [
            "7.6\nL-PDU reception\n",
            "Some ordinary paragraph of prose that happens to mention 7.6 in passing.\n",
            "2013-03-15\n4.1.1\nAUTOSAR\nAdministration\n",
            "7.12.1\nDevelopment Errors\n",
            "A\nNot applicable requirements\n",
        ]
    )
    headings = find_headings(document)
    assert [heading.path for heading in headings] == [
        "7.6 L-PDU reception",
        "7.12.1 Development Errors",
        "A Not applicable requirements",
    ]
    assert heading_for_offset(headings, 0).number == "7.6"
    assert heading_for_offset(headings, len(document.text) - 1).number == "A"
    assert heading_for_offset([], 0) is None


def test_locate_span_returns_none_across_a_page_break():
    document = document_from_blocks(["first page text\n", "second page text\n"], pages=2)
    within = document.page(1).blocks[0]
    page, bbox = locate_span(document, within.char_start, within.char_end)
    assert page == 1 and bbox == within.bbox

    page, bbox = locate_span(document, within.char_start, len(document.text))
    assert page == 1 and bbox is None


def test_merge_spans_collapses_overlaps_and_sorts():
    assert merge_spans([(10, 20), (0, 5), (18, 25), (5, 7)]) == [(0, 7), (10, 25)]
    assert merge_spans([]) == []
