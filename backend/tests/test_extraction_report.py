"""Tests for ``data/extraction_report.md`` rendering.

The report is the artifact that justifies the deterministic-only extraction
decision, so the things asserted here are the things a reviewer would look
for: the per-document counts against the manifest, and a miss list that names
every unmatched body opener with enough context to judge it.
"""

from __future__ import annotations

from pathlib import Path

from core.manifest import load_manifest
from ingestion.context_chunker import extract_context_chunks
from ingestion.extraction_report import (
    DocumentIngestion,
    render_extraction_report,
    write_extraction_report,
)
from ingestion.req_extractor import extract_requirements
from tests.support_extraction import document_from_blocks, document_from_golden

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")
CAN_DRIVER = next(doc for doc in MANIFEST.documents if doc.key == "can_driver")


def ingestion_for(document, entry=CAN_DRIVER) -> DocumentIngestion:
    extraction = extract_requirements(document, MANIFEST, entry)
    chunks, stats = extract_context_chunks(document, MANIFEST, entry, extraction)
    return DocumentIngestion(entry, extraction, chunks, stats)


def test_the_report_states_the_counts_and_the_manifest_expectation():
    ingestion = ingestion_for(document_from_golden("can_driver_p032"))
    report = render_extraction_report(MANIFEST, [ingestion])

    assert "# Extraction report" in report
    assert "## can_driver — Specification of CAN Driver" in report
    assert "requirements extracted: 9" in report
    assert "`expected_requirements` 240" in report
    assert MANIFEST.extraction.req_id_pattern in report, "the patterns used must be on the record"
    assert "### Misses" in report
    assert "### Context chunks" in report
    assert "### Distinct named symbols" in report
    assert "`Can_SetControllerMode`" in report


def test_the_report_names_and_justifies_every_unmatched_opener():
    document = document_from_blocks(
        [
            "[SWS_Can_00001] ⌈A real requirement.⌋()\n",
            "11 Not applicable requirements\n",
            "[SWS_Can_NA] ⌈These requirements are not applicable to this specification.⌋"
            "(SRS_BSW_00162)\n",
        ]
    )
    report = render_extraction_report(MANIFEST, [ingestion_for(document)])

    assert "1 body opener(s) produced no requirement" in report
    assert "`SWS_Can_NA` does not match `req_id_pattern`" in report
    assert "not applicable to this specification" in report, "the miss needs quoted context"


def test_a_clean_document_says_so_explicitly():
    document = document_from_blocks(["[SWS_Can_00001] ⌈A real requirement.⌋()\n"])
    report = render_extraction_report(MANIFEST, [ingestion_for(document)])
    assert "No unmatched body openers" in report


def test_a_miss_whose_id_does_match_the_pattern_is_flagged_for_investigation():
    """An opener with a *valid* id nearby is a real defect, not an exclusion.

    Reproduced by pushing the gap between the id and the opener past
    ``TITLE_MAX_CHARS``, which is what a genuinely mis-tuned window would look
    like — as opposed to the corpus's real misses, whose ids fail the pattern.
    """
    filler = "prose that is not a title. " * 8  # 216 chars: over TITLE_MAX_CHARS (200)
    assert 200 < len(filler) < 260  # ... but inside the miss report's lookback window
    document = document_from_blocks([f"[SWS_Can_00042] {filler}⌈an orphaned body⌋\n"])
    report = render_extraction_report(MANIFEST, [ingestion_for(document)])
    assert "**does** match `req_id_pattern` — investigate" in report


def test_the_report_is_written_where_it_is_asked_to_be(tmp_path: Path):
    document = document_from_golden("can_driver_p032")
    destination = write_extraction_report(
        MANIFEST, [ingestion_for(document)], tmp_path / "sub" / "extraction_report.md"
    )
    assert destination.is_file()
    assert destination.read_text(encoding="utf-8").startswith("# Extraction report")


def test_the_summary_table_totals_every_document():
    driver = ingestion_for(document_from_golden("can_driver_p032"))
    interface_entry = next(doc for doc in MANIFEST.documents if doc.key == "can_interface")
    interface = ingestion_for(document_from_golden("can_interface_p034"), interface_entry)
    report = render_extraction_report(MANIFEST, [driver, interface])

    assert "| can_driver | 32 |" in report
    assert "| can_interface | 34 |" in report
    assert "| **total** | | 16 | **16** | 638 | -622 |" in report
