"""Tests for ingestion/code_indexer.py (S1.4.2).

Every expectation is pinned against the hand-written fixtures in
``tests/fixtures/code/`` — no file here is copied from the GPL-2.0 corpus.

The spans matter more than they look: ``line_span`` is both a third of the
storage key (ruling R16) and what the code viewer highlights, so an off-by-one
is invisible in a smoke test and obvious in a demo. Each span is therefore
checked against the fixture's *actual* lines rather than against a second copy
of the same arithmetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core import db
from core.manifest import load_manifest
from ingestion import code_indexer
from ingestion.code_indexer import (
    ATTACH_GAP_LINES,
    DECLARATION_SUFFIX,
    FILE_SCOPE_SUFFIX,
    FILE_UNIT_KIND,
    PROTOTYPE_KIND,
    breadcrumb,
    c_parser,
    declarator_name,
    index_file,
    index_repo,
    index_source,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST_PATH = REPO_ROOT / "projects" / "autosar-can" / "project.yaml"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "code"

GIT_SHA = "09433770bebb8f27a7b480d7c96d814c68ffed3e"
PROJECT = "autosar-can"


@pytest.fixture
def manifest():
    return load_manifest(REAL_MANIFEST_PATH)


@pytest.fixture
def code(manifest):
    return manifest.code


def index_fixture(name: str, code) -> code_indexer.FileIndexResult:
    return index_file(
        FIXTURE_DIR / name, FIXTURE_DIR, code=code, git_sha=GIT_SHA, project_id=PROJECT
    )


def source_lines(name: str) -> list[str]:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------------
# the acceptance criterion: golden C fixture -> expected CodeUnits
# --------------------------------------------------------------------------

#: (kind, symbol, line_span) for every unit ``fixture_sample.c`` must yield,
#: in the order the indexer returns them.
EXPECTED_SAMPLE_UNITS = [
    (FILE_UNIT_KIND, "fixture_sample.c", (9, 10)),
    ("macro", "FIXTURE_MAX_CHANNELS", (15, 16)),
    ("macro", "FIXTURE_IS_VALID", (18, 18)),
    ("typedef", "Fixture_PduType", (20, 24)),
    ("typedef", "Fixture_StateType", (26, 29)),
    ("struct", "Fixture_Global", (31, 33)),
    ("typedef", "Fixture_GlobalType", (35, 35)),
    ("function", "Fixture_Transmit", (39, 53)),
    ("function", "Fixture_MainFunction", (55, 59)),
    ("function", "Fixture_Unreferenced", (64, 66)),
]


def test_fixture_yields_exactly_the_expected_code_units(code):
    result = index_fixture("fixture_sample.c", code)
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == EXPECTED_SAMPLE_UNITS


def test_every_unit_carries_the_manifests_language_sha_and_project(code):
    for unit in index_fixture("fixture_sample.c", code).units:
        assert unit.language == code.language == "c"
        assert unit.git_sha == GIT_SHA
        assert unit.project_id == PROJECT
        assert unit.repo_path == "fixture_sample.c"


@pytest.mark.parametrize(("kind", "symbol", "span"), EXPECTED_SAMPLE_UNITS)
def test_line_spans_are_one_based_inclusive(code, kind: str, symbol: str, span: tuple[int, int]):
    """The text under a span must be exactly the fixture's lines at that span."""
    lines = source_lines("fixture_sample.c")
    unit = next(
        u
        for u in index_fixture("fixture_sample.c", code).units
        if (u.kind, u.symbol, u.line_span) == (kind, symbol, span)
    )
    body = unit.text.split("\n", 1)[1]  # drop the breadcrumb header
    assert body == "\n".join(lines[span[0] - 1 : span[1]])
    assert body.splitlines()[0] == lines[span[0] - 1]
    assert body.splitlines()[-1] == lines[span[1] - 1]


def test_kinds_cover_struct_enum_typedef_macro_and_function(code):
    units = {u.symbol: u for u in index_fixture("fixture_sample.c", code).units}
    assert units["Fixture_Global"].kind == "struct"
    assert units["Fixture_StateType"].kind == "typedef"
    assert units["Fixture_PduType"].kind == "typedef"
    assert units["FIXTURE_MAX_CHANNELS"].kind == "macro"
    assert units["FIXTURE_IS_VALID"].kind == "macro"
    assert units["Fixture_Transmit"].kind == "function"


def test_a_named_union_gets_its_own_kind(code):
    """Folding unions into "struct" would be a silent mislabel; kind is a key."""
    result = index_source(
        "union Fixture_Word {\n    int as_int;\n    char as_bytes[4];\n};\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == [
        ("union", "Fixture_Word", (1, 4))
    ]


def test_an_enum_body_produces_an_enum_kind_when_it_is_named(code):
    result = index_source(
        "enum Fixture_Colour {\n    RED,\n    GREEN\n};\n",
        "enum.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == [
        ("enum", "Fixture_Colour", (1, 4))
    ]


# --------------------------------------------------------------------------
# trap 2: the name comes from the declarator
# --------------------------------------------------------------------------


def test_typedefd_anonymous_struct_is_named_for_the_typedef_not_a_field_type(code):
    """A naive first-identifier walk returns ``unsigned char``'s field here."""
    result = index_source(
        "typedef struct {\n    unsigned char a;\n} Can_PduType;\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert [(u.kind, u.symbol) for u in result.units] == [("typedef", "Can_PduType")]


def test_typedefd_anonymous_enum_is_named_for_the_typedef(code):
    result = index_source(
        "typedef enum {\n    A_FIRST = 0,\n    A_SECOND\n} Fixture_EnumType;\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert [(u.kind, u.symbol) for u in result.units] == [("typedef", "Fixture_EnumType")]


def test_a_named_record_and_the_typedef_naming_it_both_produce_units(code):
    """`typedef struct Foo {...} FooType;` defines two things code refers to."""
    result = index_source(
        "typedef struct Fixture_Inner {\n    int a;\n} Fixture_InnerType;\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == [
        ("struct", "Fixture_Inner", (1, 3)),
        ("typedef", "Fixture_InnerType", (1, 3)),
    ]


def test_a_symbol_repeated_in_one_file_is_separated_by_kind_and_span(code):
    """Trap 1 (R16): (file, symbol) is not a unique identity; the full key is."""
    result = index_source(
        "struct Fixture_Global {\n    int a;\n};\n\n"
        "typedef struct Fixture_Global Fixture_Global;\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    keys = [(u.repo_path, u.kind, u.symbol, u.line_span) for u in result.units]
    assert len(keys) == len(set(keys)) == 2
    assert {u.symbol for u in result.units} == {"Fixture_Global"}
    assert {u.kind for u in result.units} == {"struct", "typedef"}


def test_a_function_pointer_typedef_is_named_correctly(code):
    result = index_fixture("fixture_sample.h", code)
    unit = next(u for u in result.units if u.kind == "typedef")
    assert unit.symbol == "Fixture_CallbackType"


def test_declarator_name_returns_none_rather_than_guessing(code):
    """A nameless unit is better than a wrongly named one."""
    root = c_parser().parse(b"int (*)(void);\n").root_node
    declaration = next(n for n in root.named_children if n.type == "declaration")
    assert declarator_name(declaration.child_by_field_name("declarator")) is None


def test_variable_declarations_are_not_indexed(code):
    result = index_source(
        "static int fixtureCounter = 0;\nstatic int fixtureTable[4];\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert result.units == []


# --------------------------------------------------------------------------
# breadcrumbs, doc comments and prototypes
# --------------------------------------------------------------------------


def test_breadcrumb_header_format(code):
    unit = next(
        u for u in index_fixture("fixture_sample.c", code).units if u.symbol == "Fixture_Transmit"
    )
    assert unit.text.splitlines()[0] == "// fixture_sample.c > Fixture_Transmit"
    assert breadcrumb("communication/CanIf/src/CanIf.c", "CanIf_Transmit") == (
        "// CanIf.c > CanIf_Transmit"
    )


def test_the_attached_doc_comment_is_part_of_the_unit(code):
    unit = next(
        u for u in index_fixture("fixture_sample.c", code).units if u.symbol == "Fixture_Transmit"
    )
    assert unit.line_span[0] == 39  # the `/**` line, not the `int Fixture_...` line
    assert "Transmit one PDU." in unit.text
    assert "@req 4.0.3/CANIF661" in unit.text


def test_a_comment_separated_by_a_blank_line_is_not_swallowed(code):
    result = index_source(
        "/* detached */\n\nvoid f(void)\n{\n    return;\n}\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert result.units[0].line_span == (3, 6)


def test_header_prototypes_produce_prototype_units(code):
    result = index_fixture("fixture_sample.h", code)
    prototypes = [u for u in result.units if u.kind == PROTOTYPE_KIND]
    assert [(u.symbol, u.line_span) for u in prototypes] == [
        ("Fixture_Transmit", (9, 10)),
        ("Fixture_MainFunction", (12, 13)),
    ]
    for unit in prototypes:
        assert unit.text.splitlines()[0].endswith(DECLARATION_SUFFIX)
    # nothing in a header of only declarations may claim to be a definition
    assert not any(u.kind == "function" for u in result.units)


def test_a_declaration_and_a_definition_are_different_kinds(code):
    """The judge decides "is this implemented"; a prototype is not evidence of it.

    The distinction has to live in ``kind``, which is persisted and queryable,
    not only in the breadcrumb prose that the reranker embeds.
    """
    definition = next(
        u for u in index_fixture("fixture_sample.c", code).units if u.symbol == "Fixture_Transmit"
    )
    declaration = next(
        u for u in index_fixture("fixture_sample.h", code).units if u.symbol == "Fixture_Transmit"
    )
    assert definition.kind == "function"
    assert declaration.kind == "prototype"
    assert PROTOTYPE_KIND == "prototype"
    # the same symbol, told apart without reading either unit's text
    assert definition.symbol == declaration.symbol


def test_a_definition_is_not_marked_as_a_declaration(code):
    unit = next(
        u for u in index_fixture("fixture_sample.c", code).units if u.symbol == "Fixture_Transmit"
    )
    assert unit.kind == "function"
    assert DECLARATION_SUFFIX not in unit.text.splitlines()[0]


def test_a_static_forward_declaration_in_a_c_file_is_a_prototype(code):
    result = index_source(
        "static void f(void);\n\nstatic void f(void)\n{\n    return;\n}\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == [
        ("prototype", "f", (1, 1)),
        ("function", "f", (3, 6)),
    ]


# --------------------------------------------------------------------------
# trivial units
# --------------------------------------------------------------------------


def test_an_empty_unannotated_function_is_skipped(code):
    result = index_fixture("fixture_sample.c", code)
    assert "Fixture_Empty" not in {u.symbol for u in result.units}
    assert result.skipped_trivial == 1


def test_an_empty_but_annotated_function_is_never_skipped(code):
    """`Fixture_Unreferenced` has an empty body and one `!req` above it."""
    unit = next(
        u
        for u in index_fixture("fixture_sample.c", code).units
        if u.symbol == "Fixture_Unreferenced"
    )
    assert [a.canonical_id for a in unit.req_annotations] == ["SWS_CANIF_00142"]


def test_a_valueless_include_guard_define_is_skipped(code):
    result = index_fixture("fixture_sample.h", code)
    assert "FIXTURE_SAMPLE_H" not in {u.symbol for u in result.units}
    assert result.skipped_trivial == 1


def test_a_one_line_macro_with_a_value_is_kept(code):
    result = index_source(
        "#define FIXTURE_A 1\n", "t.c", code=code, git_sha=GIT_SHA, project_id=PROJECT
    )
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == [
        ("macro", "FIXTURE_A", (1, 1))
    ]


# --------------------------------------------------------------------------
# annotation attachment (S1.4.3's other half)
# --------------------------------------------------------------------------


def test_an_annotation_before_a_function_attaches_to_it(code):
    unit = next(
        u
        for u in index_fixture("fixture_sample.c", code).units
        if u.symbol == "Fixture_MainFunction"
    )
    assert [(a.canonical_id, a.line) for a in unit.req_annotations] == [("SWS_CanTp_00133", 55)]


def test_an_annotation_inside_a_body_attaches_and_keeps_its_own_line(code):
    unit = next(
        u for u in index_fixture("fixture_sample.c", code).units if u.symbol == "Fixture_Transmit"
    )
    assert [(a.canonical_id, a.line) for a in unit.req_annotations] == [
        ("SWS_CANIF_00661", 42),
        ("SWS_Can_00416", 43),
        ("SWS_CANIF_00316", 48),
        ("SWS_ZZZ_00001", 51),
    ]


def test_an_annotation_within_the_gap_above_a_unit_attaches_to_it(code):
    body = "\n" * (ATTACH_GAP_LINES - 1)
    result = index_source(
        f"/* @req CANIF001 */\n{body}void f(void)\n{{\n    return;\n}}\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert result.file_scope_annotations == 0
    assert [a.canonical_id for a in result.units[0].req_annotations] == ["SWS_CANIF_00001"]


def test_an_annotation_beyond_the_gap_becomes_a_file_scope_unit(code):
    body = "\n" * (ATTACH_GAP_LINES + 2)
    result = index_source(
        f"/* @req CANIF001 */\n{body}void f(void)\n{{\n    return;\n}}\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert result.file_scope_annotations == 1
    file_unit = next(u for u in result.units if u.symbol == "t.c")
    assert file_unit.kind == FILE_UNIT_KIND == "file"
    assert file_unit.line_span == (1, 1)
    assert file_unit.text.splitlines()[0].endswith(FILE_SCOPE_SUFFIX)
    assert [a.canonical_id for a in file_unit.req_annotations] == ["SWS_CANIF_00001"]


def test_file_header_annotations_are_not_dropped(code):
    """The fixture's header block belongs to no definition; it must survive."""
    result = index_fixture("fixture_sample.c", code)
    file_unit = next(u for u in result.units if u.symbol == "fixture_sample.c")
    assert result.file_scope_annotations == 2
    assert [(a.marker, a.canonical_id, a.line) for a in file_unit.req_annotations] == [
        ("@", "SWS_CANIF_00672", 9),
        ("!", "SWS_CANIF_00058", 10),
    ]


def test_every_annotation_found_ends_up_on_exactly_one_unit(code):
    for name in ("fixture_sample.c", "fixture_sample.h", "fixture_broken.c"):
        result = index_fixture(name, code)
        attached = [a for u in result.units for a in u.req_annotations]
        assert len(attached) == result.annotations, name
        assert result.claimed + result.not_claimed == result.annotations


def test_an_annotation_on_a_variable_declaration_is_not_lost(code):
    """Variables are not indexed, so their annotations go to file scope."""
    result = index_source(
        "/* @req CANIF001 */\nstatic int fixtureCounter = 0;\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert result.file_scope_annotations == 1
    assert [a.canonical_id for a in result.units[0].req_annotations] == ["SWS_CANIF_00001"]


def test_distant_file_scope_annotations_do_not_share_one_unit(code):
    source = "/* @req CANIF001 */\n" + "\n" * 40 + "/* @req CANIF002 */\n"
    result = index_source(source, "t.c", code=code, git_sha=GIT_SHA, project_id=PROJECT)
    assert len(result.units) == 2
    assert [u.line_span for u in result.units] == [(1, 1), (42, 42)]
    assert all(u.symbol == "t.c" for u in result.units)


def test_indexer_warnings_carry_the_scanners_warnings(code):
    result = index_fixture("fixture_sample.c", code)
    assert len(result.warnings) == 1
    assert "ZZZ" in result.warnings[0]
    assert "fixture_sample.c:51" in result.warnings[0]


# --------------------------------------------------------------------------
# damaged files
# --------------------------------------------------------------------------


def test_a_file_with_a_syntax_error_still_yields_its_parseable_units(code):
    result = index_fixture("fixture_broken.c", code)
    assert result.partial is True
    assert result.error_nodes > 0
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == [
        ("function", "Fixture_Before", (8, 12)),
        ("function", "Fixture_After", (20, 24)),
    ]
    assert [a.canonical_id for u in result.units for a in u.req_annotations] == [
        "SWS_CANIF_00700",
        "SWS_CANIF_00701",
    ]


def test_a_clean_file_is_not_reported_as_partial(code):
    assert index_fixture("fixture_sample.c", code).partial is False
    assert index_fixture("fixture_sample.h", code).error_nodes == 0


def test_definitions_inside_preprocessor_branches_are_found(code):
    result = index_source(
        "#ifdef FIXTURE_ON\n/* @req CANIF001 */\nvoid f(void)\n{\n    return;\n}\n#endif\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    assert [(u.kind, u.symbol, u.line_span) for u in result.units] == [("function", "f", (2, 6))]
    assert [a.canonical_id for a in result.units[0].req_annotations] == ["SWS_CANIF_00001"]


def test_a_form_feed_does_not_shift_the_spans_below_it(code):
    """`splitlines()` breaks on \\f; tree-sitter rows count only \\n.

    A form feed is routine page-break punctuation in old C, and mixing the two
    line definitions would slide every span after it by one — a code viewer
    highlighting the wrong lines, invisible to any test that does not use one.
    """
    source = (
        "void a(void)\n{\n    return;\n}\n"
        "\f\n"
        "/* @req CANIF001 */\nvoid b(void)\n{\n    return;\n}\n"
    )
    result = index_source(source, "t.c", code=code, git_sha=GIT_SHA, project_id=PROJECT)
    lines = source.split("\n")

    assert [(u.symbol, u.line_span) for u in result.units] == [
        ("a", (1, 4)),
        ("b", (6, 10)),
    ]
    unit_b = result.units[1]
    body = unit_b.text.split("\n", 1)[1]
    assert body == "\n".join(lines[5:10])
    assert body.startswith("/* @req CANIF001 */")
    assert "void b(void)" in body
    assert [a.line for a in unit_b.req_annotations] == [6]


def test_undecodable_bytes_do_not_cost_the_file_its_units(code, tmp_path: Path):
    path = tmp_path / "latin.c"
    path.write_bytes(b"/* @req CANIF001 caf\xe9 */\nvoid f(void)\n{\n    return;\n}\n")
    result = index_file(path, tmp_path, code=code, git_sha=GIT_SHA, project_id=PROJECT)
    assert [u.symbol for u in result.units] == ["f"]
    assert result.annotations == 1


# --------------------------------------------------------------------------
# determinism and storage
# --------------------------------------------------------------------------


def fingerprint(units) -> list[tuple]:
    return [
        (u.repo_path, u.kind, u.symbol, u.line_span, u.text,
         tuple((a.marker, a.canonical_id, a.line) for a in u.req_annotations))
        for u in units
    ]


def test_indexing_the_same_bytes_twice_is_identical(code):
    """Kind, span and text feed the storage key and the embedding cache."""
    for name in ("fixture_sample.c", "fixture_sample.h", "fixture_broken.c"):
        first = index_fixture(name, code)
        second = index_fixture(name, code)
        assert fingerprint(first.units) == fingerprint(second.units), name


def test_units_are_returned_in_a_total_order(code):
    units = index_fixture("fixture_sample.c", code).units
    keys = [(u.line_span[0], u.line_span[1], u.kind, u.symbol) for u in units]
    assert keys == sorted(keys)


def test_storage_keys_are_unique_within_a_file(code):
    for name in ("fixture_sample.c", "fixture_sample.h", "fixture_broken.c"):
        units = index_fixture(name, code).units
        keys = [(u.project_id, u.repo_path, u.kind, u.symbol, u.line_span) for u in units]
        assert len(set(keys)) == len(keys), name


def test_units_round_trip_through_sqlite(code, tmp_path: Path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    units = index_fixture("fixture_sample.c", code).units
    for unit in units:
        db.upsert_code_unit(conn, unit)
    assert conn.execute("SELECT COUNT(*) FROM code_units").fetchone()[0] == len(units)

    for unit in units:  # re-ingesting the same snapshot must not duplicate rows
        db.upsert_code_unit(conn, unit)
    assert conn.execute("SELECT COUNT(*) FROM code_units").fetchone()[0] == len(units)

    expected = next(u for u in units if u.symbol == "Fixture_Transmit")
    (loaded,) = db.list_code_units_by_symbol(conn, PROJECT, "Fixture_Transmit")
    assert loaded == expected
    conn.close()


def test_a_repeated_symbol_survives_a_round_trip_as_two_rows(code, tmp_path: Path):
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    result = index_source(
        "struct Fixture_Dup {\n    int a;\n};\n\ntypedef struct Fixture_Dup Fixture_Dup;\n",
        "t.c",
        code=code,
        git_sha=GIT_SHA,
        project_id=PROJECT,
    )
    for unit in result.units:
        db.upsert_code_unit(conn, unit)
    loaded = db.list_code_units_by_symbol(conn, PROJECT, "Fixture_Dup")
    assert len(loaded) == 2
    assert {u.kind for u in loaded} == {"struct", "typedef"}
    conn.close()


# --------------------------------------------------------------------------
# whole-directory indexing
# --------------------------------------------------------------------------


def test_index_repo_walks_the_files_it_is_given(manifest):
    files = sorted(FIXTURE_DIR.glob("*.[ch]"))
    seen: list[tuple[str, int, int | None]] = []
    result = index_repo(
        manifest, FIXTURE_DIR, files=files, on_progress=lambda *a: seen.append(a)
    )

    assert [r.repo_path for r in result.files] == [p.name for p in files]
    assert [name for name, _, _ in seen] == [p.name for p in files]
    assert [done for _, done, _ in seen] == list(range(1, len(files) + 1))
    assert result.annotations == 14
    assert result.claimed == 10
    assert result.not_claimed == 4
    assert result.file_scope_annotations == 2
    assert result.partial_files == ["fixture_broken.c"]
    assert len(result.warnings) == 1
    assert result.units
    assert all(u.git_sha == manifest.code.git_sha for u in result.units)


def test_index_repo_canonical_ids_are_deduplicated(manifest):
    result = index_repo(manifest, FIXTURE_DIR, files=sorted(FIXTURE_DIR.glob("*.[ch]")))
    ids = result.canonical_ids
    assert len(ids) == len(set(ids))
    assert "SWS_Can_00416" in ids
    assert "SWS_CanTp_00133" in ids


def test_index_repo_calls_on_file_for_every_file(manifest):
    files = sorted(FIXTURE_DIR.glob("*.[ch]"))
    seen: list[str] = []
    index_repo(manifest, FIXTURE_DIR, files=files, on_file=lambda r: seen.append(r.repo_path))
    assert seen == [p.name for p in files]
