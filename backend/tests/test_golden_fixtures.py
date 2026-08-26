"""Tests for the golden fixtures (S1.3.4).

These tests assert the *fixtures themselves* are valid and internally
consistent. D4 adds the extractor-vs-fixture recall test; nothing here runs
an extractor, and nothing here needs ``data/`` — the fixtures must pass on a
fresh clone (controller ruling R11).

The strongest check is
:func:`test_expected_text_occurs_verbatim_on_the_page`: every hand-labeled
requirement text must be findable in the frozen parser output under the
normalization the README documents. That catches a transcription slip in a
hand-labeled file without turning the fixture into the output of a program.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from core.manifest import load_manifest
from core.models import Requirement
from ingestion.text_norm import extract_named_symbols, normalize_requirement_text

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST_PATH = REPO_ROOT / "projects" / "autosar-can" / "project.yaml"
GOLDEN_DIR = Path(__file__).resolve().parent / "fixtures" / "golden"
INDEX_PATH = GOLDEN_DIR / "fixtures.json"

MANIFEST = load_manifest(REAL_MANIFEST_PATH)
DOCUMENTS = {doc.key: doc for doc in MANIFEST.documents}
INDEX = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
FIXTURES: list[dict[str, Any]] = INDEX["pages"]
FIXTURE_IDS = [entry["fixture"] for entry in FIXTURES]

UPSTREAM_RE = re.compile(rf"^{MANIFEST.extraction.upstream_id_pattern}$")
SYMBOL_RE = re.compile(MANIFEST.extraction.symbol_pattern)
FILENAME_RE = re.compile(r"^(?P<key>[a-z_]+)_p(?P<page>\d{3})$")


def load(fixture: str, half: str) -> dict[str, Any]:
    return json.loads((GOLDEN_DIR / f"{fixture}.{half}.json").read_text(encoding="utf-8"))


def normalize(text: str, document_title: str) -> str:
    """The normalization contract, from its single implementation.

    ``ingestion.text_norm`` is the spec (ratified as binding on D4's
    extractor); this wrapper only binds it to the real manifest so the tests
    read cleanly. It is deliberately *not* a second copy of the rules.
    """
    return normalize_requirement_text(
        text,
        footer_patterns=MANIFEST.extraction.footer_patterns,
        drop_markers=MANIFEST.extraction.drop_markers,
        document_title=document_title,
    )


@pytest.fixture(params=FIXTURES, ids=FIXTURE_IDS)
def fixture_entry(request: pytest.FixtureRequest) -> dict[str, Any]:
    return request.param


# --------------------------------------------------------------------------
# the index and the file set
# --------------------------------------------------------------------------


def test_index_matches_the_committed_files():
    listed = {entry["fixture"] for entry in FIXTURES}
    on_disk = {path.name.removesuffix(".blocks.json") for path in GOLDEN_DIR.glob("*.blocks.json")}
    expected_on_disk = {
        path.name.removesuffix(".expected.json") for path in GOLDEN_DIR.glob("*.expected.json")
    }
    assert listed == on_disk, "fixtures.json and the .blocks.json files disagree"
    assert listed == expected_on_disk, "fixtures.json and the .expected.json files disagree"


def test_index_covers_both_documents_and_the_page_break_pair():
    keys = {entry["document_key"] for entry in FIXTURES}
    assert {"can_driver", "can_interface"} <= keys, (
        "both requirement-ID casings must be represented or a casing bug passes every test"
    )
    driver_pages = {e["page"] for e in FIXTURES if e["document_key"] == "can_driver"}
    assert {34, 35} <= driver_pages, "the page-break case is only meaningful as a pair"
    assert len(FIXTURES) >= 3


def test_every_fixture_names_a_known_document_and_has_a_reason(fixture_entry: dict[str, Any]):
    assert fixture_entry["document_key"] in DOCUMENTS
    assert fixture_entry["why"].strip()
    match = FILENAME_RE.match(fixture_entry["fixture"])
    assert match, f"{fixture_entry['fixture']}: name must be <doc_key>_p<NNN>"
    assert match.group("key") == fixture_entry["document_key"]
    assert int(match.group("page")) == fixture_entry["page"]


# --------------------------------------------------------------------------
# .blocks.json — the frozen parser output
# --------------------------------------------------------------------------


def test_blocks_fixture_is_well_formed(fixture_entry: dict[str, Any]):
    blocks_file = load(fixture_entry["fixture"], "blocks")
    document = DOCUMENTS[fixture_entry["document_key"]]

    assert blocks_file["fixture"] == fixture_entry["fixture"]
    assert blocks_file["document_key"] == fixture_entry["document_key"]
    assert blocks_file["source_document"] == document.filename
    assert blocks_file["page"] == fixture_entry["page"]
    assert 1 <= blocks_file["page"] <= blocks_file["page_count"]
    assert blocks_file["corpus_version"] == MANIFEST.version
    assert re.fullmatch(r"[0-9a-f]{64}", blocks_file["source_sha256"])
    assert blocks_file["blocks"], "a frozen page with no blocks proves nothing"

    parser = blocks_file["parser"]
    assert parser["library"] == "pymupdf"
    assert re.fullmatch(r"\d+\.\d+\.\d+", parser["version"])
    assert parser["extraction_mode"] == "blocks"


def test_blocks_have_ordered_bboxes_inside_the_page(fixture_entry: dict[str, Any]):
    blocks_file = load(fixture_entry["fixture"], "blocks")
    width, height = blocks_file["width"], blocks_file["height"]
    for block in blocks_file["blocks"]:
        x0, y0, x1, y1 = block["bbox"]
        assert len(block["bbox"]) == 4
        assert x0 <= x1 and y0 <= y1, f"{block['block_no']}: bbox not ordered"
        assert -1.0 <= x0 and x1 <= width + 1.0, f"{block['block_no']}: bbox outside the page"
        assert -1.0 <= y0 and y1 <= height + 1.0, f"{block['block_no']}: bbox outside the page"
        # Every frozen bbox must be a legal Requirement.bbox.
        Requirement.model_validate(
            {
                "id": "SWS_Probe_00001",
                "text": block["text"],
                "page": blocks_file["page"],
                "bbox": block["bbox"],
                "source_doc": blocks_file["source_document"],
                "version": MANIFEST.version,
                "project_id": MANIFEST.project_id,
            }
        )


def test_block_char_spans_are_contiguous_and_size_matched(fixture_entry: dict[str, Any]):
    blocks_file = load(fixture_entry["fixture"], "blocks")
    assert blocks_file["blocks"][0]["char_start"] == blocks_file["char_start"]
    assert blocks_file["blocks"][-1]["char_end"] == blocks_file["char_end"]
    for block in blocks_file["blocks"]:
        assert block["char_end"] - block["char_start"] == len(block["text"])
        assert block["text"], "empty blocks must be dropped by the parser"
        assert block["text"].endswith("\n"), "block text must be newline-terminated"
    for previous, current in zip(blocks_file["blocks"], blocks_file["blocks"][1:], strict=False):
        assert previous["char_end"] == current["char_start"]


def test_the_page_break_pair_is_char_adjacent():
    """Page 35 must start exactly where page 34 ends, or char_span is useless."""
    first = load("can_driver_p034", "blocks")
    second = load("can_driver_p035", "blocks")
    assert first["page"] + 1 == second["page"]
    assert first["char_end"] == second["char_start"]
    assert first["source_sha256"] == second["source_sha256"]


# --------------------------------------------------------------------------
# .expected.json — the hand-labeled ground truth
# --------------------------------------------------------------------------


def test_expected_fixture_is_well_formed(fixture_entry: dict[str, Any]):
    expected = load(fixture_entry["fixture"], "expected")
    document = DOCUMENTS[fixture_entry["document_key"]]

    assert expected["fixture"] == fixture_entry["fixture"]
    assert expected["document_key"] == fixture_entry["document_key"]
    assert expected["source_document"] == document.filename
    assert expected["page"] == fixture_entry["page"]
    assert "hand-verified" in expected["labeled"]
    assert expected["notes"].strip()
    assert isinstance(expected["requirements"], list)


def test_expected_requirements_are_valid_and_unique(fixture_entry: dict[str, Any]):
    expected = load(fixture_entry["fixture"], "expected")
    document = DOCUMENTS[fixture_entry["document_key"]]
    id_re = re.compile(rf"^{document.req_id_pattern}$")

    ids = [req["id"] for req in expected["requirements"]]
    assert len(ids) == len(set(ids)), f"duplicate ids in {fixture_entry['fixture']}: {ids}"

    for req in expected["requirements"]:
        assert id_re.match(req["id"]), (
            f"{req['id']} does not match this document's req_id_pattern "
            f"{document.req_id_pattern!r}"
        )
        assert req["text"].strip(), f"{req['id']}: empty text"
        assert req["text"] == req["text"].strip()
        assert "\n" not in req["text"], f"{req['id']}: labeled text must be whitespace-collapsed"
        assert req["title"] is None or req["title"].strip() == req["title"] != ""
        assert req["page"] == fixture_entry["page"]
        assert isinstance(req["spans_page_break"], bool)
        for upstream in req["upstream_ids"]:
            assert UPSTREAM_RE.match(upstream), f"{req['id']}: bad upstream id {upstream!r}"
        assert len(req["upstream_ids"]) == len(set(req["upstream_ids"]))
        assert len(req["named_symbols"]) == len(set(req["named_symbols"]))


def test_expected_requirements_construct_real_requirement_models(fixture_entry: dict[str, Any]):
    """The labels must be loadable into the model D2 froze, or they are useless."""
    expected = load(fixture_entry["fixture"], "expected")
    for req in expected["requirements"]:
        model = Requirement.model_validate(
            {
                "id": req["id"],
                "title": req["title"],
                "text": req["text"],
                "page": req["page"],
                "bbox": None if req["spans_page_break"] else [0.0, 0.0, 1.0, 1.0],
                "source_doc": expected["source_document"],
                "upstream_ids": req["upstream_ids"],
                "named_symbols": req["named_symbols"],
                "version": MANIFEST.version,
                "project_id": MANIFEST.project_id,
            }
        )
        assert model.id == req["id"]
        assert Requirement.canonical_id(model.id) == req["id"].upper()


def test_named_symbols_match_the_manifest_symbol_pattern(fixture_entry: dict[str, Any]):
    expected = load(fixture_entry["fixture"], "expected")
    for req in expected["requirements"]:
        for symbol in req["named_symbols"]:
            assert SYMBOL_RE.fullmatch(symbol), (
                f"{req['id']}: {symbol!r} does not match the manifest symbol_pattern"
            )
            assert symbol in req["text"], f"{req['id']}: {symbol!r} is not in the labeled text"


def test_named_symbols_are_complete_not_merely_correct(fixture_entry: dict[str, Any]):
    """No symbol in the labeled text may be missing from ``named_symbols``.

    The other test only proves every *listed* symbol is real. This one closes
    the other direction: the list must equal every match of the manifest's
    ``symbol_pattern`` in the labeled text, in first-appearance order,
    deduplicated — the rule ``ingestion.text_norm`` defines and D4 will use.
    """
    expected = load(fixture_entry["fixture"], "expected")
    for req in expected["requirements"]:
        derived = extract_named_symbols(req["text"], MANIFEST.extraction.symbol_pattern)
        assert req["named_symbols"] == derived, (
            f"{req['id']}: named_symbols is {req['named_symbols']} but the labeled text "
            f"yields {derived} — a symbol was missed, duplicated, or mis-ordered"
        )


def test_labeled_ids_are_unique_across_the_whole_fixture_set():
    """SWS_Can_00407 must be labeled once, on the page its id appears on."""
    seen: dict[str, str] = {}
    for entry in FIXTURES:
        expected = load(entry["fixture"], "expected")
        for req in expected["requirements"]:
            key = f"{entry['document_key']}:{req['id']}"
            assert key not in seen, (
                f"{req['id']} is labeled in both {seen.get(key)} and {entry['fixture']}"
            )
            seen[key] = entry["fixture"]
    assert seen["can_driver:SWS_Can_00407"] == "can_driver_p034"


# --------------------------------------------------------------------------
# the two halves against each other
# --------------------------------------------------------------------------


def test_expected_text_occurs_verbatim_on_the_page(fixture_entry: dict[str, Any]):
    """Every labeled text must be findable in the frozen parser output."""
    expected = load(fixture_entry["fixture"], "expected")
    blocks_file = load(fixture_entry["fixture"], "blocks")
    document = DOCUMENTS[fixture_entry["document_key"]]

    raw = "".join(block["text"] for block in blocks_file["blocks"])
    # A requirement that spans the page break is only complete across the pair.
    following = next(
        (
            e
            for e in FIXTURES
            if e["document_key"] == fixture_entry["document_key"]
            and e["page"] == fixture_entry["page"] + 1
        ),
        None,
    )
    if following is not None:
        raw += "".join(block["text"] for block in load(following["fixture"], "blocks")["blocks"])
    haystack = normalize(raw, document.title)

    for req in expected["requirements"]:
        assert req["id"] in haystack, f"{req['id']}: id not present on the page"
        assert req["text"] in haystack, (
            f"{req['id']}: labeled text not found in the frozen page output — "
            "either the label has a transcription error or the parser output changed"
        )
        if req["title"] is not None:
            assert f"[{req['id']}] {req['title']} " in haystack, (
                f"{req['id']}: labeled title not found between the id and the body opener"
            )


def test_requirement_count_equals_the_body_openers_on_the_page(fixture_entry: dict[str, Any]):
    """One '⌈' on the page == one requirement labeled for the page.

    Holds for every page in this set: no fixture page has a requirement whose
    id sits on one page and whose body opener sits on the next.
    """
    expected = load(fixture_entry["fixture"], "expected")
    blocks_file = load(fixture_entry["fixture"], "blocks")
    raw = "".join(block["text"] for block in blocks_file["blocks"])
    openers = raw.count(MANIFEST.extraction.body_open)
    assert openers == len(expected["requirements"]), (
        f"{fixture_entry['fixture']}: {openers} '{MANIFEST.extraction.body_open}' on the page "
        f"but {len(expected['requirements'])} labeled requirements"
    )


def test_the_tracing_table_page_is_the_false_positive_guard():
    """Page 23 is a table of SWS ids that are references, not definitions."""
    expected = load("can_driver_p023", "expected")
    blocks_file = load("can_driver_p023", "blocks")
    raw = "".join(block["text"] for block in blocks_file["blocks"])

    assert expected["requirements"] == []
    assert MANIFEST.extraction.body_open not in raw
    assert MANIFEST.extraction.body_close not in raw
    # It really is full of ids that a naive '\[(SWS_...)\]' scan would grab.
    assert len(re.findall(rf"\[{DOCUMENTS['can_driver'].req_id_pattern}\]", raw)) > 20
    for marker in MANIFEST.extraction.drop_markers:
        assert marker in raw, f"{marker} should still be present in the frozen page"


def test_the_constr_page_needs_the_infix_in_the_id_pattern():
    """A pattern without '(?:_CONSTR)?' must fail to match these ids."""
    expected = load("can_driver_p094", "expected")
    naive = re.compile(r"^SWS_Can_\d+$")
    ids = [req["id"] for req in expected["requirements"]]
    assert ids and all("_CONSTR_" in req_id for req_id in ids)
    assert not any(naive.match(req_id) for req_id in ids)


def test_the_titled_variant_keeps_the_source_documents_misspelling():
    """'Definiton' is misspelled in the source; the label must not fix it."""
    expected = load("can_driver_p043", "expected")
    titles = {req["id"]: req["title"] for req in expected["requirements"] if req["title"]}
    assert titles == {
        "SWS_Can_91019": "Definiton of development errors in module Can",
        "SWS_Can_91020": "Definiton of runtime errors in module Can",
    }


def test_the_page_break_requirement_is_labeled_as_spanning():
    expected = load("can_driver_p034", "expected")
    spanning = [req for req in expected["requirements"] if req["spans_page_break"]]
    assert [req["id"] for req in spanning] == ["SWS_Can_00407"]
    # Its floor is on page 35 and must not create a second requirement there.
    page35 = load("can_driver_p035", "expected")
    assert "SWS_Can_00407" not in {req["id"] for req in page35["requirements"]}
    raw35 = "".join(b["text"] for b in load("can_driver_p035", "blocks")["blocks"])
    assert raw35.count(MANIFEST.extraction.body_close) == len(page35["requirements"]) + 1


def test_both_id_casings_are_actually_present():
    driver = load("can_driver_p032", "expected")
    interface = load("can_interface_p034", "expected")
    assert all(req["id"].startswith("SWS_Can_") for req in driver["requirements"])
    assert all(req["id"].startswith("SWS_CANIF_") for req in interface["requirements"])


def test_the_fixture_set_carries_upstream_ids_at_all():
    """A fixture set where every upstream list is empty would not test tracing."""
    all_upstream = [
        upstream
        for entry in FIXTURES
        for req in load(entry["fixture"], "expected")["requirements"]
        for upstream in req["upstream_ids"]
    ]
    assert len(all_upstream) >= 15
    assert len({u.split("_")[1] for u in all_upstream}) >= 2  # SRS_BSW_*, SRS_Can_*, SRS_SPAL_*


def test_readme_documents_the_normalization_and_the_hand_label_rule():
    readme = (GOLDEN_DIR / "README.md").read_text(encoding="utf-8")
    assert "regenerate.py" in readme
    assert "never" in readme.lower()
    assert ".expected.json" in readme
