"""Tests for context-prose chunking (story S1.3.5).

The acceptance criterion is :func:`test_a_fixture_page_yields_both_chunk_types`
— a real fixture page must produce both ``doc_type="requirement"`` and
``doc_type="context"`` records, because the self-query ``doc_type`` filter in
the retrieval pipeline (S2.4.1) is worthless if one of the two never exists.
"""

from __future__ import annotations

from pathlib import Path

from core.manifest import load_manifest
from ingestion.context_chunker import (
    MAX_CAPTION_CHARS,
    MAX_CHUNK_CHARS,
    MIN_CHUNK_CHARS,
    caption_extent,
    excluded_section_roots,
    extract_context_chunks,
    is_excluded_section,
    subtract_spans,
)
from ingestion.req_extractor import Heading, extract_requirements
from tests.support_extraction import document_from_blocks, document_from_golden

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
DOCUMENTS = {doc.key: doc for doc in MANIFEST.documents}
CAN_DRIVER = DOCUMENTS["can_driver"]

#: A paragraph long enough to clear MIN_CHUNK_CHARS on its own.
LONG_PROSE = (
    "The Can module provides services for initiating transmissions and calls the "
    "callback functions of the CanIf module for notifying events, independently from "
    "the notification method (interrupt or polling). Several CAN controllers can be "
    "controlled by a single Can module as long as they belong to the same CAN "
    "Hardware Unit."
)
MORE_PROSE = (
    "For a closer description of the CAN controller and the CAN Hardware Unit, see "
    "the chapter on acronyms and abbreviations. The Can module offers a hardware "
    "abstraction so that upper layers never touch a register directly, which is what "
    "makes the driver portable across microcontroller families."
)


def chunk_for(document, entry=CAN_DRIVER):
    extraction = extract_requirements(document, MANIFEST, entry)
    chunks, stats = extract_context_chunks(document, MANIFEST, entry, extraction)
    return extraction, chunks, stats


# --------------------------------------------------------------------------
# the acceptance criterion
# --------------------------------------------------------------------------


def test_a_fixture_page_yields_both_chunk_types():
    """CAN Driver page 32 carries nine requirements and prose between them."""
    extraction, chunks, _ = chunk_for(document_from_golden("can_driver_p032"))

    assert extraction.requirements, "the page defines requirements"
    assert chunks, "the page also carries prose that must become context chunks"
    assert {req.doc_type for req in extraction.requirements} == {"requirement"}
    assert {chunk.doc_type for chunk in chunks} == {"context"}


def test_context_chunk_ids_are_stable_across_runs():
    """Unstable ids mean the embedding cache never hits on re-ingest."""
    first = [chunk.id for chunk in chunk_for(document_from_golden("can_driver_p032"))[1]]
    second = [chunk.id for chunk in chunk_for(document_from_golden("can_driver_p032"))[1]]
    assert first == second
    assert first, "there is nothing to compare if no chunk was produced"
    assert len(first) == len(set(first)), f"duplicate context chunk ids: {first}"
    assert all(chunk_id.startswith("CTX_can_driver_") for chunk_id in first)


def test_no_chunk_is_shorter_than_the_minimum_or_longer_than_the_cap():
    for fixture in ("can_driver_p032", "can_driver_p035", "can_interface_p034"):
        entry = CAN_DRIVER if fixture.startswith("can_driver") else DOCUMENTS["can_interface"]
        _, chunks, _ = chunk_for(document_from_golden(fixture), entry)
        for chunk in chunks:
            assert MIN_CHUNK_CHARS <= len(chunk.text) <= MAX_CHUNK_CHARS, (
                f"{fixture}/{chunk.id}: {len(chunk.text)} characters is outside "
                f"{MIN_CHUNK_CHARS}–{MAX_CHUNK_CHARS}"
            )


def test_no_character_of_a_requirement_body_appears_in_a_context_chunk():
    """The two pools must partition the document, not overlap it."""
    for fixture in ("can_driver_p032", "can_driver_p035", "can_driver_p043"):
        document = document_from_golden(fixture)
        extraction, chunks, _ = chunk_for(document)
        requirement_spans = [req.char_span for req in extraction.requirements]

        for chunk in chunks:
            start, end = chunk.char_span
            for req_start, req_end in requirement_spans:
                assert not (start < req_end and end > req_start), (
                    f"{fixture}/{chunk.id} span {chunk.char_span} overlaps requirement span "
                    f"{(req_start, req_end)}"
                )
        # And no body sentence leaks in verbatim either.
        bodies = [req.text for req in extraction.requirements]
        for chunk in chunks:
            for body in bodies:
                assert body not in chunk.text, f"{fixture}/{chunk.id} repeats a requirement body"


def test_a_chunk_never_splits_mid_sentence_at_a_paragraph_boundary():
    document = document_from_blocks([LONG_PROSE + "\n", MORE_PROSE + "\n"])
    _, chunks, _ = chunk_for(document)
    assert chunks
    for chunk in chunks:
        assert chunk.text[-1] in ".!?:;)", f"{chunk.id} ends mid-sentence: {chunk.text[-60:]!r}"


# --------------------------------------------------------------------------
# the skip rules
# --------------------------------------------------------------------------


def test_figure_and_table_captions_are_excluded():
    caption = (
        "Figure 7.4: CanTpNTa, CanTpNSa and CanTpNAe configuration overview, showing how "
        "the addressing formats relate to the configuration containers."
    )
    # Long enough that the minimum-length rule is not what removes it, short
    # enough to stay inside the caption bound.
    assert MIN_CHUNK_CHARS < len(caption) < MAX_CAPTION_CHARS
    document = document_from_blocks(["1\nIntroduction\n", LONG_PROSE + "\n", caption + "\n"])
    _, chunks, stats = chunk_for(document)

    assert stats.caption_residues_stripped == 1
    assert stats.caption_remainders_kept == 0, "a caption-only residue leaves nothing behind"
    assert all("configuration overview" not in chunk.text for chunk in chunks)
    assert any("Can module provides services" in chunk.text for chunk in chunks)


def test_table_captions_are_excluded_too():
    caption = (
        "Table 3: Development errors of the Can module, listing every error code "
        "together with the API service that is able to raise it."
    )
    assert MIN_CHUNK_CHARS < len(caption) < MAX_CAPTION_CHARS
    document = document_from_blocks(["1\nIntroduction\n", caption + "\n", LONG_PROSE + "\n"])
    _, chunks, stats = chunk_for(document)
    assert stats.caption_residues_stripped == 1
    assert all("Development errors of the Can module" not in chunk.text for chunk in chunks)


def test_a_multi_line_caption_is_kept_whole_rather_than_fragmented():
    """The real shape whose end cannot be told from a fused paragraph's.

    Verbatim from CAN Driver page 36, line breaks included: a caption whose
    first line has no terminator and whose third line does. That is exactly
    the shape a period-less caption fused onto a paragraph takes, so it is
    kept whole as prose (case 3). The outcome that must *not* happen is a
    mid-caption fragment — stripping just the first line would index
    'numbering of HTHs and HRHs are implementation specific. …' on its own.
    """
    caption = (
        "Figure 7.3: Example of assignment of HTHs and HRHs to the Hardware Objects. The\n"
        "numbering of HTHs and HRHs are implementation specific. The chosen numbering is\n"
        "only an example.\n"
    )
    document = document_from_blocks(["1\nIntroduction\n", LONG_PROSE + "\n", caption])
    _, chunks, stats = chunk_for(document)

    assert stats.caption_residues_stripped == 0
    assert stats.caption_residues_kept_whole == 1
    fragments = [c.text for c in chunks if "numbering of HTHs" in c.text]
    assert fragments, "the caption text must survive somewhere, not be dropped"
    assert all(text.startswith("Figure 7.3:") for text in fragments), (
        "the caption must be kept whole, never split mid-caption"
    )


def test_prose_fused_onto_a_caption_survives_the_caption_strip():
    """The regression this rule was rewritten for.

    When PyMuPDF fuses a caption with the paragraph that follows it — the same
    block-fusion failure mode the p035 fixture exists to capture — the old rule
    discarded the whole residue and the paragraph vanished with no record. Only
    the caption may be removed.
    """
    fused = (
        "Figure 7.1: Layered Software Architecture from the Can point of view.\n"
        + LONG_PROSE
        + "\n"
    )
    document = document_from_blocks(["1\nIntroduction\n", fused])
    _, chunks, stats = chunk_for(document)

    assert stats.caption_residues_stripped == 1
    assert stats.caption_remainders_kept == 1
    assert stats.caption_remainder_chars >= MIN_CHUNK_CHARS
    assert chunks, "the fused paragraph must survive as a chunk"
    assert any("Can module provides services" in chunk.text for chunk in chunks), (
        "the paragraph fused onto the caption was silently deleted"
    )
    assert all("Layered Software Architecture" not in chunk.text for chunk in chunks), (
        "the caption itself must still be removed"
    )
    # The surviving chunk starts at the prose, not mid-caption.
    survivor = next(chunk for chunk in chunks if "Can module provides" in chunk.text)
    assert survivor.text.startswith("The Can module provides services")


def test_a_caption_without_a_period_fused_onto_prose_keeps_the_prose():
    """The round-1 fix's own blind spot, one level deeper.

    Every earlier test used a caption whose first line ends in a period, which
    takes the unambiguous path. Here the caption line has **no** terminator and
    the fused paragraph's first sentence spans three physical lines, so the
    old line-walking extent consumed the caption *and the whole paragraph* and
    dropped both — with `caption_remainders_kept` staying 0, so the counters
    showed nothing wrong. The residue must now be kept whole as prose.
    """
    fused = (
        "Figure 9.2: Message routing\n"
        "The module forwards frames between the two configured\n"
        "CAN channels without modification, as specified in the safety\n"
        "manual, section 4, which is reproduced at length here so the residue "
        "comfortably clears the minimum chunk size.\n"
    )
    document = document_from_blocks(["1\nIntroduction\n", fused])
    _, chunks, stats = chunk_for(document)

    assert stats.caption_residues_stripped == 0, "the caption's end is not identifiable here"
    assert stats.caption_residues_kept_whole == 1
    assert chunks, "the paragraph must not be dropped"
    assert any("forwards frames between the two configured" in chunk.text for chunk in chunks), (
        "the paragraph fused onto a period-less caption was silently deleted"
    )
    # Kept whole means the caption rides along as prose — noise, not loss.
    assert any("Message routing" in chunk.text for chunk in chunks)


def test_a_period_less_caption_alone_is_still_stripped():
    """Keeping-whole must not become the rule for ordinary single-line captions.

    97 of the corpus's 102 captions are exactly this shape.
    """
    document = document_from_blocks(
        [
            "1\nIntroduction\n",
            "Figure 10.1: Overview about CAN Interface configuration containers\n",
            LONG_PROSE + "\n",
        ]
    )
    _, chunks, stats = chunk_for(document)

    assert stats.caption_residues_stripped == 1
    assert stats.caption_residues_kept_whole == 0
    assert all("Overview about CAN Interface" not in chunk.text for chunk in chunks)
    assert any("Can module provides services" in chunk.text for chunk in chunks)


def test_an_overlong_caption_like_residue_is_kept_as_prose():
    """A long paragraph that merely starts like a caption must not be eaten."""
    long_residue = "Table 4: " + "the quick brown fox jumps over the lazy dog " * 12
    assert len(long_residue) > MAX_CAPTION_CHARS
    document = document_from_blocks(["1\nIntroduction\n", long_residue + "\n"])
    _, chunks, stats = chunk_for(document)

    assert caption_extent(long_residue + "\n") == 0
    assert stats.caption_residues_stripped == 0
    assert stats.caption_residues_kept_whole == 1
    assert any("quick brown fox" in chunk.text for chunk in chunks)


def test_caption_extent_decides_the_three_cases():
    """Case 1 first line, case 2 whole residue, case 3 keep everything."""
    # Case 1 — the caption line ends a sentence: the caption is that line, and
    # what follows is a different paragraph.
    fused = "Figure 7.1: Layered Software Architecture.\nThe Can module provides services.\n"
    assert caption_extent(fused) == len("Figure 7.1: Layered Software Architecture.\n")

    # Case 2 — nothing in the residue ends a sentence, so there is no paragraph
    # after the caption to lose. 97 of the corpus's 102 captions look like this.
    single = "Figure 10.1: Overview about CAN Interface configuration containers\n"
    assert caption_extent(single) == len(single)

    # Case 3 — a *later* line ends a sentence. Could be a two-line caption
    # (this real one from CAN Driver page 39 is) or a paragraph fused onto a
    # period-less caption. Indistinguishable, so keep it all as prose.
    wrapped = (
        "Figure 7.5: Example of assignment of same HRHs to multiple Objects The\n"
        "chosen numbering is only an example.\n"
    )
    assert caption_extent(wrapped) == 0

    # The bound, and the degenerate input.
    assert caption_extent("Table 4: " + "x " * MAX_CAPTION_CHARS) == 0
    assert caption_extent("") == 0


def test_front_matter_before_the_first_heading_is_excluded():
    """The table of contents, change-history table and disclaimer all live here."""
    toc = (
        "1 Introduction and functional Overview 13 2 Acronyms and Abbreviations 14 2.1 "
        "Priority Inversion 15 2.2 CAN Hardware Unit 17 3 Related Documentation 18 3.1 "
        "Related specification 18 4 Constraints and assumptions 19 4.1 Limitations 19"
    )
    history = (
        "Document Change History Date Release Changed by Description 2013-03-15 4.1.1 "
        "AUTOSAR Administration Added support for Pretended Networking and corrected the "
        "sequence for EcuM_SetWakeupEvent in section 7.7 of this specification."
    )
    document = document_from_blocks(
        [toc + "\n", history + "\n", "1\nIntroduction\n", LONG_PROSE + "\n"]
    )
    _, chunks, stats = chunk_for(document)

    assert stats.blocks_front_matter == 2
    assert all("Priority Inversion 15" not in chunk.text for chunk in chunks)
    assert all("Document Change History" not in chunk.text for chunk in chunks)
    assert any("Can module provides services" in chunk.text for chunk in chunks)


def test_the_bibliography_section_and_its_subsections_are_excluded():
    bibliography = (
        "[1] Specification of CAN Driver AUTOSAR_CP_SWS_CANDriver [2] Specification of CAN "
        "Transceiver Driver AUTOSAR_CP_SWS_CANTransceiverDriver [3] Requirements on CAN "
        "AUTOSAR_CP_SRS_CAN [4] Specification of ECU Configuration AUTOSAR_CP_TPS_ECU"
    )
    document = document_from_blocks(
        [
            "1\nIntroduction\n",
            LONG_PROSE + "\n",
            "3\nRelated Documentation\n",
            bibliography + "\n",
            "3.1\nRelated specification\n",
            bibliography + "\n",
            "4\nConstraints and assumptions\n",
            MORE_PROSE + "\n",
        ]
    )
    _, chunks, stats = chunk_for(document)

    assert stats.blocks_excluded_sections == {"bibliography": 2}, (
        "section 3 and its subsection 3.1 must both be excluded"
    )
    assert all("AUTOSAR_CP_SWS_CANDriver" not in chunk.text for chunk in chunks)
    assert any("hardware abstraction" in chunk.text for chunk in chunks)


def test_the_change_history_annex_and_its_subsections_are_excluded():
    items = (
        "Added Specification Items in R23-11 SWS_CANIF_00001 SWS_CANIF_00002 "
        "SWS_CANIF_00003 SWS_CANIF_00004 SWS_CANIF_00005 SWS_CANIF_00006 SWS_CANIF_00007 "
        "SWS_CANIF_00008 SWS_CANIF_00009 SWS_CANIF_00010 SWS_CANIF_00011 SWS_CANIF_00012"
    )
    document = document_from_blocks(
        [
            "1\nIntroduction\n",
            LONG_PROSE + "\n",
            "B\nChange History\n",
            items + "\n",
            "B.1.1\nAdded Specification Items in R23-11\n",
            items + "\n",
        ]
    )
    _, chunks, stats = chunk_for(document)
    assert stats.blocks_excluded_sections == {"change_history": 2}
    assert all("Added Specification Items" not in chunk.text for chunk in chunks)


def test_isolated_short_residue_is_dropped_rather_than_indexed():
    """A stray bullet or table cell with no neighbour to merge into is noise.

    Note the deliberate asymmetry: a short residue that is *adjacent* to real
    prose merges into it (nothing is lost). Only a residue standing alone —
    here fenced off by requirement blocks on both sides — is dropped.
    """
    document = document_from_blocks(
        [
            "1\nIntroduction\n",
            "[SWS_Can_00001] ⌈The Can module shall enter the STOPPED state.⌋()\n",
            "• SLEEP -> STOPPED\n",
            "[SWS_Can_00002] ⌈The Can module shall leave the SLEEP state.⌋()\n",
            LONG_PROSE + "\n",
        ]
    )
    _, chunks, stats = chunk_for(document)

    assert stats.chunks_dropped_short == 1
    assert all(len(chunk.text) >= MIN_CHUNK_CHARS for chunk in chunks)
    assert all("SLEEP -> STOPPED" not in chunk.text for chunk in chunks)
    assert any("Can module provides services" in chunk.text for chunk in chunks)


def test_a_heading_becomes_metadata_rather_than_prose():
    document = document_from_blocks(["7.6\nL-PDU reception\n", LONG_PROSE + "\n"])
    _, chunks, stats = chunk_for(document)

    assert stats.blocks_headings == 1
    assert chunks[0].section_path == "7.6 L-PDU reception"
    assert chunks[0].title == "L-PDU reception"
    assert not chunks[0].text.startswith("L-PDU reception")


def test_context_chunks_carry_named_symbols_but_never_upstream_ids():
    document = document_from_blocks(
        [
            "1\nIntroduction\n",
            "The upper layer calls Can_Write and the Can module answers with "
            "CanIf_TxConfirmation once the hardware has accepted the frame, which is the "
            "notification path described in the rest of this chapter and repeated here at "
            "length so that the paragraph clears the minimum chunk size.\n",
        ]
    )
    _, chunks, _ = chunk_for(document)
    assert chunks[0].named_symbols == ["Can_Write", "CanIf_TxConfirmation"]
    assert chunks[0].upstream_ids == []


def test_an_oversized_paragraph_is_split_at_a_sentence_boundary():
    sentence = "The Can module shall notify the CanIf module about the completed transmission. "
    document = document_from_blocks(["1\nIntroduction\n", sentence * 40 + "\n"])
    _, chunks, _ = chunk_for(document)

    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk.text) <= MAX_CHUNK_CHARS
        assert chunk.text.endswith("transmission.")


def test_a_page_spanning_chunk_gets_no_bbox():
    document = document_from_blocks([LONG_PROSE + "\n", MORE_PROSE + "\n"], pages=2)
    _, chunks, _ = chunk_for(document)
    assert chunks
    for chunk in chunks:
        pages = document.pages_for_span(*chunk.char_span)
        if len(pages) > 1:
            assert chunk.bbox is None
        else:
            assert chunk.bbox is not None


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_subtract_spans_leaves_the_gaps():
    assert subtract_spans(0, 100, [(10, 20), (50, 60)]) == [(0, 10), (20, 50), (60, 100)]
    assert subtract_spans(0, 100, [(0, 100)]) == []
    assert subtract_spans(0, 100, []) == [(0, 100)]
    assert subtract_spans(30, 40, [(0, 100)]) == []
    assert subtract_spans(0, 50, [(40, 200)]) == [(0, 40)]


def test_excluded_section_roots_and_descendants():
    headings = [
        Heading("1", "Introduction", 0, 1),
        Heading("3", "Related documentation", 100, 2),
        Heading("3.1", "Input documents & related standards and norms", 200, 2),
        Heading("31", "Something else entirely", 300, 3),
        Heading("B", "Change history of AUTOSAR traceable items", 400, 4),
    ]
    roots = excluded_section_roots(headings)
    assert roots == {"3": "bibliography", "B": "change_history"}
    assert is_excluded_section(headings[0], roots) is None
    assert is_excluded_section(headings[1], roots) == "bibliography"
    assert is_excluded_section(headings[2], roots) == "bibliography"
    assert is_excluded_section(headings[3], roots) is None, "'31' is not a child of '3'"
    assert is_excluded_section(headings[4], roots) == "change_history"
    assert is_excluded_section(None, roots) is None
