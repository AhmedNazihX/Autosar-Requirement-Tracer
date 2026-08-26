"""Tests for ingestion/annotations.py (S1.4.3, rulings R6 and R12).

Two things are being pinned here. **R6:** the code is annotated in the AUTOSAR
4.0.3 style (``CANIF023``) while the ingested specs are R23-11
(``SWS_CANIF_00023``), so canonicalization is the only reason tier-1 evidence
joins anything at all. **R12:** ``@req`` and ``!req`` mean *opposite* things,
and the last test in this file is the one that stops a future refactor from
quietly collapsing them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.manifest import load_manifest
from core.models import CodeUnit, ReqAnnotation
from ingestion.annotations import (
    AnnotationError,
    canonicalize,
    line_at,
    line_offsets,
    scan_annotations,
    scan_text,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST_PATH = REPO_ROOT / "projects" / "autosar-can" / "project.yaml"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "code"


@pytest.fixture
def code():
    return load_manifest(REAL_MANIFEST_PATH).code


def scan_one(source: str, code) -> ReqAnnotation:
    result = scan_annotations(source, code)
    assert len(result.annotations) == 1, result.annotations
    return result.annotations[0]


# --------------------------------------------------------------------------
# the shapes that actually occur in the corpus
# --------------------------------------------------------------------------


def test_at_req_with_release_prefix(code):
    annotation = scan_one("/* @req 4.0.3/CANIF661 */\n", code)
    assert annotation.canonical_id == "SWS_CANIF_00661"
    assert annotation.claim == "claimed_implemented"
    assert annotation.marker == "@"
    assert annotation.raw == "@req 4.0.3/CANIF661"
    assert annotation.line == 1


def test_bang_req_with_release_prefix_is_a_negative_claim(code):
    annotation = scan_one("/* !req 4.0.3/CANIF316 */\n", code)
    assert annotation.canonical_id == "SWS_CANIF_00316"
    assert annotation.claim == "claimed_not_implemented"
    assert annotation.marker == "!"


def test_bang_req_without_release_prefix_parses(code):
    annotation = scan_one("/* !req CANIF058 */\n", code)
    assert annotation.canonical_id == "SWS_CANIF_00058"
    assert annotation.claim == "claimed_not_implemented"
    assert annotation.raw == "!req CANIF058"


def test_cross_module_annotation_keeps_the_module_maps_casing(code):
    """A CanIf file citing a CAN Driver requirement must canonicalize to it."""
    annotation = scan_one("/* @req 4.3.0/CAN416 */\n", code)
    assert annotation.canonical_id == "SWS_Can_00416"


def test_annotation_inside_a_doc_comment_block_is_found(code):
    source = "/**\n * Do a thing.\n *\n * @req CANTP133\n */\nvoid f(void) {}\n"
    annotation = scan_one(source, code)
    assert annotation.canonical_id == "SWS_CanTp_00133"
    assert annotation.line == 4


def test_two_annotations_on_one_line_are_both_found(code):
    result = scan_annotations("/* @req 4.0.3/CANIF023 *//* !req 4.0.3/CANIF142 */\n", code)
    assert [a.canonical_id for a in result.annotations] == [
        "SWS_CANIF_00023",
        "SWS_CANIF_00142",
    ]
    assert [a.line for a in result.annotations] == [1, 1]


def test_numbers_are_zero_padded_to_five_digits(code):
    assert scan_one("/* @req CANIF5 */\n", code).canonical_id == "SWS_CANIF_00005"
    assert scan_one("/* @req CANIF91025 */\n", code).canonical_id == "SWS_CANIF_91025"


def test_line_numbers_are_one_based_and_per_annotation(code):
    source = "\n\n/* @req CANIF001 */\nvoid f(void)\n{\n    /* !req CANIF002 */\n}\n"
    result = scan_annotations(source, code)
    assert [(a.canonical_id, a.line) for a in result.annotations] == [
        ("SWS_CANIF_00001", 3),
        ("SWS_CANIF_00002", 6),
    ]


def test_text_without_annotations_yields_nothing(code):
    result = scan_annotations("/* nothing to see, @requirement is not a marker */\n", code)
    assert result.annotations == []
    assert result.warnings == []


# --------------------------------------------------------------------------
# drift and misconfiguration
# --------------------------------------------------------------------------


def test_unmapped_module_prefix_warns_and_preserves_the_raw_value(code):
    result = scan_annotations("/* @req 9.9.9/ZZZ001 */\n", code, source="Zzz.c")
    (annotation,) = result.annotations
    assert annotation.raw == "@req 9.9.9/ZZZ001"
    assert annotation.canonical_id == "SWS_ZZZ_00001"
    assert len(result.warnings) == 1
    warning = result.warnings[0]
    assert "ZZZ" in warning
    assert "annotation_module_map" in warning
    assert "Zzz.c:1" in warning


def test_module_lookup_is_case_insensitive(code):
    assert scan_one("/* @req CanIf023 */\n", code).canonical_id == "SWS_CANIF_00023"
    assert scan_one("/* @req canif023 */\n", code).canonical_id == "SWS_CANIF_00023"


def test_canonicalize_returns_no_warning_for_a_known_module():
    canonical, warning = canonicalize(
        "CANTP", "133", module_map={"CANTP": "CanTp"}, id_template="SWS_{module}_{num:05d}"
    )
    assert canonical == "SWS_CanTp_00133"
    assert warning is None


def test_a_pattern_missing_a_named_group_is_a_manifest_error(code):
    with pytest.raises(AnnotationError) as excinfo:
        scan_text(
            "/* @req CANIF001 */",
            pattern=r"@req\s+\w+",
            markers=code.annotation_markers,
            module_map=code.annotation_module_map,
            id_template=code.annotation_id_template,
        )
    assert "marker" in str(excinfo.value)
    assert "annotation_pattern" in str(excinfo.value)


def test_an_unmapped_marker_is_a_manifest_error_not_a_silent_drop(code):
    """Dropping an annotation is the one outcome R12 exists to prevent."""
    with pytest.raises(AnnotationError) as excinfo:
        scan_text(
            "/* !req CANIF058 */",
            pattern=code.annotation_pattern,
            markers={"@": "claimed_implemented"},
            module_map=code.annotation_module_map,
            id_template=code.annotation_id_template,
        )
    assert "annotation_markers" in str(excinfo.value)


def test_an_unusable_id_template_is_a_manifest_error(code):
    with pytest.raises(AnnotationError) as excinfo:
        scan_text(
            "/* @req CANIF001 */",
            pattern=code.annotation_pattern,
            markers=code.annotation_markers,
            module_map=code.annotation_module_map,
            id_template="SWS_{nonesuch}",
        )
    assert "annotation_id_template" in str(excinfo.value)


def test_a_claim_the_model_rejects_is_a_manifest_error(code):
    with pytest.raises(AnnotationError) as excinfo:
        scan_text(
            "/* @req CANIF001 */",
            pattern=code.annotation_pattern,
            markers={"@": "probably_implemented"},
            module_map=code.annotation_module_map,
            id_template=code.annotation_id_template,
        )
    assert "cannot build an annotation" in str(excinfo.value)


# --------------------------------------------------------------------------
# the fixture, and R12 end to end
# --------------------------------------------------------------------------


def test_the_hand_written_fixture_yields_every_shape(code):
    source = (FIXTURE_DIR / "fixture_sample.c").read_text(encoding="utf-8")
    result = scan_annotations(source, code, source="fixture_sample.c")

    assert len(result.annotations) == 10
    assert len(result.claimed) == 7
    assert len(result.not_claimed) == 3
    assert [(a.marker, a.canonical_id) for a in result.annotations] == [
        ("@", "SWS_CANIF_00672"),
        ("!", "SWS_CANIF_00058"),
        ("@", "SWS_CANIF_00010"),
        ("@", "SWS_CANIF_00020"),
        ("@", "SWS_CANIF_00661"),
        ("@", "SWS_Can_00416"),
        ("!", "SWS_CANIF_00316"),
        ("@", "SWS_ZZZ_00001"),
        ("@", "SWS_CanTp_00133"),
        ("!", "SWS_CANIF_00142"),
    ]


def test_claimed_implemented_ids_excludes_bang_req(code):
    """R12, asserted directly: ``!req`` must never look like evidence.

    Collapsing the markers would make the traceability report claim
    implementation for requirements the original authors documented as *not*
    implemented — 192 annotations across the in-scope files.
    """
    source = (FIXTURE_DIR / "fixture_sample.c").read_text(encoding="utf-8")
    unit = CodeUnit(
        repo_path="fixture_sample.c",
        language="c",
        symbol="Fixture_Transmit",
        kind="function",
        line_span=(1, len(source.splitlines())),
        text=source,
        req_annotations=scan_annotations(source, code).annotations,
        git_sha="0" * 40,
        project_id="autosar-can",
    )

    assert "SWS_CANIF_00661" in unit.claimed_implemented_ids
    assert "SWS_Can_00416" in unit.claimed_implemented_ids
    for negative in ("SWS_CANIF_00058", "SWS_CANIF_00316", "SWS_CANIF_00142"):
        assert any(a.canonical_id == negative for a in unit.req_annotations)
        assert negative not in unit.claimed_implemented_ids
    assert len(unit.claimed_implemented_ids) == 7


def test_scan_result_partitions_by_claim(code):
    source = "/* @req CANIF001 */\n/* !req CANIF002 */\n"
    result = scan_annotations(source, code)
    assert [a.canonical_id for a in result.claimed] == ["SWS_CANIF_00001"]
    assert [a.canonical_id for a in result.not_claimed] == ["SWS_CANIF_00002"]
    assert len(result.claimed) + len(result.not_claimed) == len(result.annotations)


# --------------------------------------------------------------------------
# line arithmetic
# --------------------------------------------------------------------------


def test_line_offsets_and_line_at():
    text = "aa\nbbbb\n\nc"
    offsets = line_offsets(text)
    assert offsets == [0, 3, 8, 9]
    assert line_at(offsets, 0) == 1
    assert line_at(offsets, 2) == 1
    assert line_at(offsets, 3) == 2
    assert line_at(offsets, 8) == 3
    assert line_at(offsets, 9) == 4
