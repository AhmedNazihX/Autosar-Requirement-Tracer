"""Unit tests for core/models.py (S1.2.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.models import CodeUnit, ReqAnnotation, Requirement

GIT_SHA = "09433770bebb8f27a7b480d7c96d814c68ffed3e"


def _requirement(**overrides) -> Requirement:
    fields = {
        "id": "SWS_Can_00011",
        "title": None,
        "text": "The Can module shall ⌈do the thing⌋.",
        "section_path": "7.6 L-PDU reception",
        "page": 42,
        "bbox": (10.0, 20.0, 100.0, 40.0),
        "char_span": None,
        "source_doc": "can_driver",
        "upstream_ids": [],
        "named_symbols": ["Can_Write"],
        "version": "R23-11",
        "project_id": "autosar-can",
    }
    fields.update(overrides)
    return Requirement(**fields)


def test_requirement_round_trip():
    req = _requirement()
    dumped = req.model_dump()
    restored = Requirement(**dumped)
    assert restored == req


def test_requirement_extra_field_forbidden():
    with pytest.raises(ValidationError):
        _requirement(not_a_real_field="oops")


def test_requirement_id_preserves_document_casing():
    can = _requirement(id="SWS_Can_00011")
    canif = _requirement(id="SWS_CANIF_CONSTR_00001", source_doc="can_interface")
    assert can.id == "SWS_Can_00011"
    assert canif.id == "SWS_CANIF_CONSTR_00001"


def test_canonical_id_uppercases_without_mutating_id():
    assert Requirement.canonical_id("sws_can_00011") == "SWS_CAN_00011"
    assert Requirement.canonical_id("SWS_CANIF_CONSTR_00001") == "SWS_CANIF_CONSTR_00001"
    req = _requirement(id="SWS_Can_00011")
    assert req.id == "SWS_Can_00011"  # unaffected by canonicalization


def test_upstream_ids_accepts_srs_and_rs_prefixes():
    req = _requirement(upstream_ids=["SRS_Can_01059", "RS_Ids_00810"])
    assert req.upstream_ids == ["SRS_Can_01059", "RS_Ids_00810"]


def test_doc_type_defaults_to_requirement():
    req = _requirement()
    assert req.doc_type == "requirement"


def test_doc_type_context_explicit():
    req = _requirement(doc_type="context")
    assert req.doc_type == "context"


def test_doc_type_rejects_invalid_value():
    with pytest.raises(ValidationError):
        _requirement(doc_type="not_a_real_type")


def test_requirement_bbox_and_char_span_both_optional():
    req = _requirement(bbox=None, char_span=(10, 25))
    assert req.bbox is None
    assert req.char_span == (10, 25)


def test_requirement_char_span_rejects_backwards_range():
    with pytest.raises(ValidationError):
        _requirement(char_span=(50, 10))


def test_requirement_bbox_rejects_backwards_rectangle():
    with pytest.raises(ValidationError):
        _requirement(bbox=(100.0, 100.0, 0.0, 0.0))


def _code_unit(**overrides) -> CodeUnit:
    fields = {
        "repo_path": "communication/CanIf/src/CanIf.c",
        "language": "c",
        "symbol": "CanIf_Transmit",
        "kind": "function",
        "line_span": (100, 150),
        "text": "void CanIf_Transmit(...) { ... }",
        "req_annotations": [],
        "git_sha": GIT_SHA,
        "project_id": "autosar-can",
    }
    fields.update(overrides)
    return CodeUnit(**fields)


def test_code_unit_round_trip():
    unit = _code_unit()
    dumped = unit.model_dump()
    restored = CodeUnit(**dumped)
    assert restored == unit


def test_code_unit_extra_field_forbidden():
    with pytest.raises(ValidationError):
        _code_unit(not_a_real_field="oops")


def test_code_unit_line_span_rejects_backwards_range():
    with pytest.raises(ValidationError):
        _code_unit(line_span=(150, 100))


def test_code_unit_git_sha_must_be_hex40():
    with pytest.raises(ValidationError):
        _code_unit(git_sha="abc")


@pytest.mark.parametrize(
    "kind",
    ["function", "prototype", "struct", "union", "enum", "typedef", "macro", "file"],
)
def test_code_unit_kind_accepts_every_member(kind):
    assert _code_unit(kind=kind).kind == kind


def test_code_unit_kind_separates_a_definition_from_a_declaration():
    """``function`` is an implementation; ``prototype`` only declares one.

    The evidence judge (spec §5) rules on whether a requirement is
    *implemented*, so this distinction has to be a persisted, queryable field
    rather than something a caller infers from a unit's text.
    """
    definition = _code_unit(kind="function")
    declaration = _code_unit(kind="prototype")
    assert definition.kind != declaration.kind


def test_code_unit_kind_rejects_unknown_value():
    with pytest.raises(ValidationError):
        _code_unit(kind="namespace")


def test_req_annotation_at_marker_means_claimed_implemented():
    ann = ReqAnnotation(
        canonical_id="SWS_CANIF_00023",
        raw="4.0.3/CANIF023",
        marker="@",
        claim="claimed_implemented",
        line=88,
    )
    assert ann.claim == "claimed_implemented"


def test_req_annotation_bang_marker_means_claimed_not_implemented():
    ann = ReqAnnotation(
        canonical_id="SWS_CANIF_00316",
        raw="4.0.3/CANIF316",
        marker="!",
        claim="claimed_not_implemented",
        line=210,
    )
    assert ann.claim == "claimed_not_implemented"


def test_req_annotation_rejects_marker_claim_mismatch():
    with pytest.raises(ValidationError):
        ReqAnnotation(
            canonical_id="SWS_CANIF_00023",
            raw="4.0.3/CANIF023",
            marker="@",
            claim="claimed_not_implemented",
            line=88,
        )


def test_code_unit_claimed_implemented_ids_excludes_bang_annotations():
    unit = _code_unit(
        req_annotations=[
            ReqAnnotation(
                canonical_id="SWS_CANIF_00023",
                raw="4.0.3/CANIF023",
                marker="@",
                claim="claimed_implemented",
                line=10,
            ),
            ReqAnnotation(
                canonical_id="SWS_CANIF_00316",
                raw="4.0.3/CANIF316",
                marker="!",
                claim="claimed_not_implemented",
                line=20,
            ),
            ReqAnnotation(
                canonical_id="SWS_CANIF_00023",
                raw="4.0.3/CANIF023",
                marker="@",
                claim="claimed_implemented",
                line=30,
            ),
        ]
    )
    assert unit.claimed_implemented_ids == ["SWS_CANIF_00023"]
