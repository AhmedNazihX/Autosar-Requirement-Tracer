"""End-to-end tests for the ingestion CLI, with no network and no OpenRouter.

The fixture corpus is a real PDF, generated with PyMuPDF at test time rather
than committed as a binary blob, so the pipeline exercises the actual
``parse_pdf`` → ``extract_requirements`` → ``extract_context_chunks`` path.
Its body delimiters are ``«…»`` instead of the real corpus's ``⌈…⌋`` for a
mundane reason: the PDF base-14 fonts have no glyph for U+2308, so a generated
fixture using it round-trips as ``?``. That the delimiters *can* differ is the
point of putting them in the manifest — this fixture is a second corpus, which
is exactly the abstraction the design spec (§2) asks for.

Network and provider access are injected (``downloader``, ``git_runner``,
``transport``), so the second-run assertions are about *call counts*: the point
is not that a second run is fast, it is that it makes no calls at all.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
import yaml

from core import db, paths
from core.config import Settings
from core.embeddings import API_KEY_ENV_VAR, EmbeddingBatch
from core.manifest import load_manifest
from ingestion import run
from ingestion.context_chunker import extract_context_chunks
from ingestion.extraction_report import DocumentIngestion, ingest_document
from ingestion.req_extractor import extract_requirements
from ingestion.spot_check import CRITERIA, render_spot_check, select_picks
from tests.support_extraction import document_from_blocks

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")

SHA = "0123456789abcdef0123456789abcdef01234567"

#: Blocks written into the fixture PDF, one ``insert_textbox`` each so PyMuPDF
#: reports them as separate blocks (a block is a paragraph in this pipeline).
PAGE_ONE = [
    "1\nIntroduction",
    "This document specifies the fixture CAN driver used by the ingestion "
    "tests. It exists so the pipeline can be exercised end to end over a real "
    "PDF without downloading the AUTOSAR standards, and it is long enough to "
    "survive the minimum context-chunk length.",
    "[SWS_Fix_00001] Can_Write shall reject a null pointer "
    "«The function Can_Write shall return E_NOT_OK when the PDU pointer is "
    "a null pointer.»(SRS_Can_01001)",
    "[SWS_Fix_00002] «The Can module shall report CAN_E_PARAM_POINTER to the "
    "Det module.»()",
]
PAGE_TWO = [
    "2\nRequirements",
    "The following constraint applies to the configuration of the fixture "
    "driver and is written out at length so that it becomes a context chunk "
    "rather than being dropped as page furniture by the minimum-length rule.",
    "[SWS_Fix_CONSTR_00003] «The number of configured hardware objects shall "
    "not exceed the number of hardware handles.»()",
    "[SWS_Fix_00004] «CanIf_RxIndication shall be called for every received "
    "frame.»(SRS_Can_01002)",
]

#: One C file for the fake repository. Deliberately includes both an annotated
#: definition and an unannotated prototype of the same symbol, which is the
#: case ``embeddable_code_units`` drops.
FIXTURE_C = """\
#ifndef FIXTURE_H
#define FIXTURE_H

/* @req 4.0.3/CAN00001 */
Std_ReturnType Can_Write(const void *pdu);

/* @req 4.0.3/CAN00001 */
Std_ReturnType Can_Write(const void *pdu) {
    if (pdu == NULL) {
        return E_NOT_OK;
    }
    return E_OK;
}

/* !req 4.0.3/CAN00099 */
void Can_MainFunction_Write(void) {
    return;
}

#endif
"""


def write_fixture_pdf(path: Path) -> None:
    """Write a two-page PDF whose text the extractor can work on."""
    document = pymupdf.open()
    for blocks in (PAGE_ONE, PAGE_TWO):
        page = document.new_page()
        top = 60.0
        for text in blocks:
            page.insert_textbox(
                pymupdf.Rect(60.0, top, 535.0, top + 160.0),
                text,
                fontsize=10,
                fontname="helv",
            )
            top += 170.0
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    document.close()


FIXTURE_MANIFEST = {
    "project_id": "fixture-can",
    "name": "Fixture CAN corpus",
    "version": "F1",
    "documents": [
        {
            "key": "fixture_driver",
            "title": "Specification of the Fixture CAN Driver",
            "url": "https://example.invalid/fixture/FixtureCANDriver.pdf",
            "filename": "FixtureCANDriver.pdf",
            "module": "Can",
            "req_id_pattern": r"SWS_Fix(?:_CONSTR)?_\d+",
            "expected_requirements": 4,
        }
    ],
    "extraction": {
        "req_id_pattern": r"SWS_[A-Za-z]+(?:_CONSTR)?_\d+",
        "upstream_id_pattern": r"(?:SRS|RS)_[A-Za-z]+_\d+",
        "symbol_pattern": r"\b((?:Can|CanIf|Det)_[A-Z][A-Za-z0-9]*)\b",
        "body_open": "«",
        "body_close": "»",
        "footer_patterns": [],
        "drop_markers": [],
    },
    "code": {
        "repo_url": "https://example.invalid/fixture/repo",
        "git_sha": SHA,
        "license": "GPL-2.0",
        "language": "c",
        "include_globs": ["communication/**/*.[ch]"],
        "exclude_globs": ["**/*_Cfg.[ch]"],
        "annotation_pattern": (
            r"(?P<marker>[@!])req\s+(?:(?P<release>[\d.]+)/)?(?P<module>[A-Za-z]+)(?P<num>\d+)"
        ),
        "annotation_markers": {"@": "claimed_implemented", "!": "claimed_not_implemented"},
        "annotation_module_map": {"CAN": "Fix"},
        "annotation_id_template": "SWS_{module}_{num:05d}",
    },
    "models": {
        "chat": "test/chat",
        "judge": "test/judge",
        "rerank": "test/rerank",
        "translate": "test/translate",
        "title": "test/title",
        "embedding": "test/embedding-v1",
    },
    "limits": {"max_report_cost_usd": 1.0},
}


class FakeDownloader:
    """Writes the pre-generated PDF bytes; counts every call."""

    def __init__(self, source: Path) -> None:
        self.source = source
        self.calls: list[str] = []

    def __call__(self, url: str, dest: Path, on_chunk=None) -> int:
        self.calls.append(url)
        payload = self.source.read_bytes()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        if on_chunk is not None:
            on_chunk(len(payload), len(payload))
        return len(payload)


class FakeGit:
    """A git runner that materialises the fixture work tree; counts every call."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd: Path) -> str:
        self.calls.append(list(args))
        command = args[0]
        git_dir = cwd / ".git"
        if command == "init":
            git_dir.mkdir(parents=True, exist_ok=True)
            (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
            return ""
        if command == "checkout":
            for relative, content in self.files.items():
                target = cwd / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            (git_dir / "HEAD").write_text(f"{SHA}\n", encoding="utf-8")
            return ""
        if command == "rev-parse":
            return f"{SHA}\n"
        return ""


class FakeTransport:
    """Deterministic vectors, and a record of every input it was sent."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    @property
    def inputs(self) -> list[str]:
        return [text for call in self.calls for text in call]

    def __call__(self, texts, model_id: str) -> EmbeddingBatch:
        self.calls.append(list(texts))
        return EmbeddingBatch(
            vectors=[[float(len(text) % 97), 0.5, 0.25] for text in texts],
            prompt_tokens=len(texts),
            total_tokens=len(texts),
            cost_usd=1e-7 * len(texts),
        )


@pytest.fixture
def corpus(tmp_path: Path):
    """A self-contained fixture repo root: ``projects/`` plus a source PDF."""
    manifest_dir = tmp_path / "projects" / "fixture-can"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "project.yaml"
    manifest_path.write_text(yaml.safe_dump(FIXTURE_MANIFEST), encoding="utf-8")

    source_pdf = tmp_path / "source" / "FixtureCANDriver.pdf"
    write_fixture_pdf(source_pdf)

    manifest = load_manifest(manifest_path)
    return manifest, manifest_path, source_pdf


def counts(db_path: Path, project_id: str) -> dict[str, int]:
    conn = db.connect(db_path)
    try:
        return {
            "requirements": conn.execute(
                "SELECT COUNT(*) AS n FROM requirements WHERE project_id = ? "
                "AND doc_type = 'requirement'",
                (project_id,),
            ).fetchone()["n"],
            "context": conn.execute(
                "SELECT COUNT(*) AS n FROM requirements WHERE project_id = ? "
                "AND doc_type = 'context'",
                (project_id,),
            ).fetchone()["n"],
            "code_units": conn.execute(
                "SELECT COUNT(*) AS n FROM code_units WHERE project_id = ?", (project_id,)
            ).fetchone()["n"],
            "embeddings": conn.execute(
                "SELECT COUNT(*) AS n FROM embedding_cache"
            ).fetchone()["n"],
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------
# the whole pipeline, twice
# --------------------------------------------------------------------------


def test_the_first_run_builds_everything(corpus):
    manifest, _, source_pdf = corpus
    downloader = FakeDownloader(source_pdf)
    git = FakeGit({"communication/Fix/Fix.c": FIXTURE_C})
    transport = FakeTransport()

    summary = run.run_ingestion(
        manifest,
        run.Options(),
        reporter=run.Reporter(quiet=True),
        downloader=downloader,
        git_runner=git,
        transport=transport,
    )

    assert len(downloader.calls) == 1, "the PDF must be fetched on a cold run"
    assert git.calls, "the repository must be checked out on a cold run"
    assert summary.documents[0].requirements == 4
    assert summary.documents[0].expected == 4
    assert summary.documents[0].context_chunks >= 1
    assert summary.code is not None and summary.code.git_sha == SHA
    assert summary.embeddings.computed == summary.embeddings.chunks > 0
    assert summary.embeddings.cached == 0
    assert summary.embeddings.upserted == summary.embeddings.chunks
    assert summary.embeddings.usage.cost_usd > 0
    assert summary.index_check is not None and summary.index_check.records > 0
    assert summary.index_check.hits >= 1, "the smoke query must find something"


def test_the_second_run_downloads_nothing_embeds_nothing_and_changes_nothing(corpus):
    """The acceptance criterion three of the composed stories share."""
    manifest, _, source_pdf = corpus
    files = {"communication/Fix/Fix.c": FIXTURE_C}
    downloader, git, transport = FakeDownloader(source_pdf), FakeGit(files), FakeTransport()
    options = run.Options()

    first = run.run_ingestion(
        manifest,
        options,
        reporter=run.Reporter(quiet=True),
        downloader=downloader,
        git_runner=git,
        transport=transport,
    )
    before = counts(paths.db_path(manifest), manifest.project_id)

    downloader.calls.clear()
    git.calls.clear()
    transport.calls.clear()

    second = run.run_ingestion(
        manifest,
        options,
        reporter=run.Reporter(quiet=True),
        downloader=downloader,
        git_runner=git,
        transport=transport,
    )

    assert downloader.calls == [], "a warm data/docs must trigger no download"
    assert git.calls == [], "a warm data/repo at the pinned SHA must not invoke git"
    assert transport.inputs == [], "a warm embedding cache must send zero inputs"

    assert second.embeddings.computed == 0
    assert second.embeddings.cached == second.embeddings.chunks == first.embeddings.chunks
    assert second.embeddings.usage.calls == 0
    assert second.embeddings.usage.cost_usd == 0.0
    assert second.embeddings.collection_count == first.embeddings.collection_count
    assert counts(paths.db_path(manifest), manifest.project_id) == before
    assert second.documents[0].fetch_action == "skipped"
    assert second.code is not None and second.code.fetch_action == "skipped"


def test_the_artifacts_are_written_where_the_gate_expects_them(corpus):
    manifest, _, source_pdf = corpus
    run.run_ingestion(
        manifest,
        run.Options(skip_embeddings=True),
        reporter=run.Reporter(quiet=True),
        downloader=FakeDownloader(source_pdf),
        git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
    )

    report = paths.extraction_report_path(manifest)
    spot = paths.spot_check_path(manifest)
    assert report.is_file() and spot.is_file()
    assert report.read_text(encoding="utf-8").startswith("# Extraction report")

    text = spot.read_text(encoding="utf-8")
    assert text.startswith("# Spot check — WP1 gate")
    assert "FixtureCANDriver.pdf" in text, "the filename must be prominent"
    assert "page " in text
    assert "SWS_Fix_" in text


def test_the_code_snapshot_is_indexed_with_its_annotations(corpus):
    manifest, _, source_pdf = corpus
    summary = run.run_ingestion(
        manifest,
        run.Options(skip_embeddings=True),
        reporter=run.Reporter(quiet=True),
        downloader=FakeDownloader(source_pdf),
        git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
    )
    assert summary.code is not None
    assert summary.code.claimed == 2
    assert summary.code.not_claimed == 1
    assert summary.join is not None
    # SWS_Fix_00001 is annotated and extracted; SWS_Fix_00099 is annotated only.
    assert summary.join.matched == 1
    assert summary.join.distinct_ids == 2


def test_skip_embeddings_leaves_sqlite_complete_and_chroma_absent(corpus):
    manifest, _, source_pdf = corpus
    summary = run.run_ingestion(
        manifest,
        run.Options(skip_embeddings=True),
        reporter=run.Reporter(quiet=True),
        downloader=FakeDownloader(source_pdf),
        git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
    )
    assert summary.embeddings.skipped
    assert "--skip-embeddings" in (summary.embeddings.reason or "")
    assert not paths.chroma_dir(manifest).exists()
    assert counts(paths.db_path(manifest), manifest.project_id)["requirements"] == 4
    assert summary.index_check is not None and summary.index_check.records > 0


def test_stop_after_halts_the_pipeline_where_asked(corpus):
    manifest, _, source_pdf = corpus
    summary = run.run_ingestion(
        manifest,
        run.Options(stop_after="extract", skip_embeddings=True),
        reporter=run.Reporter(quiet=True),
        downloader=FakeDownloader(source_pdf),
        git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
    )
    assert summary.documents and summary.code is None
    assert not paths.db_path(manifest).exists()


# --------------------------------------------------------------------------
# failures are one readable line, never a traceback
# --------------------------------------------------------------------------


def test_a_manifest_error_exits_non_zero_with_a_readable_message(tmp_path, capsys):
    bad = tmp_path / "project.yaml"
    bad.write_text("project_id: fixture\n", encoding="utf-8")

    code = run.main([str(bad)])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err.startswith("error: ")
    assert "Traceback" not in captured.err
    assert str(bad) in captured.err


def test_a_missing_manifest_file_exits_non_zero(tmp_path, capsys):
    code = run.main([str(tmp_path / "absent.yaml")])
    assert code == 1
    assert "manifest file not found" in capsys.readouterr().err


def test_extractor_warnings_fail_the_run_only_after_the_report_is_written(corpus, monkeypatch):
    """Ruling R26: report first, fail second."""
    manifest, _, source_pdf = corpus

    def duplicated(document, mf, entry):
        extraction = extract_requirements(document, mf, entry)
        object.__setattr__(
            extraction,
            "warnings",
            ["duplicate requirement ids extracted: SWS_Fix_00001"],
        )
        return extraction

    monkeypatch.setattr("ingestion.extraction_report.extract_requirements", duplicated)

    with pytest.raises(run.IngestionError) as error:
        run.run_ingestion(
            manifest,
            run.Options(skip_embeddings=True),
            reporter=run.Reporter(quiet=True),
            downloader=FakeDownloader(source_pdf),
            git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
        )

    message = str(error.value)
    assert "duplicate requirement ids" in message
    assert paths.extraction_report_path(manifest).is_file(), "the report must survive the failure"
    assert str(paths.extraction_report_path(manifest)) in message
    assert not paths.db_path(manifest).exists(), "nothing may be indexed after a warning"


def test_the_same_id_in_two_documents_fails_the_run_after_the_report(tmp_path, monkeypatch):
    """The other half of ruling R26, which no per-document pass can see.

    ``requirements``' primary key spans the corpus and the insert is
    ``ON CONFLICT DO UPDATE`` (idempotent re-ingestion needs it), so a
    cross-document collision would otherwise be silently merged — one
    requirement left wearing the other's text.
    """
    manifest_dir = tmp_path / "projects" / "fixture-can"
    manifest_dir.mkdir(parents=True)
    twice = {
        **FIXTURE_MANIFEST,
        "documents": [
            FIXTURE_MANIFEST["documents"][0],
            {
                # The same document under a second key: same ids, different key.
                **FIXTURE_MANIFEST["documents"][0],
                "key": "fixture_driver_copy",
                "filename": "FixtureCANDriverCopy.pdf",
            },
        ],
    }
    manifest_path = manifest_dir / "project.yaml"
    manifest_path.write_text(yaml.safe_dump(twice), encoding="utf-8")
    source_pdf = tmp_path / "source" / "FixtureCANDriver.pdf"
    write_fixture_pdf(source_pdf)
    manifest = load_manifest(manifest_path)

    with pytest.raises(run.IngestionError) as error:
        run.run_ingestion(
            manifest,
            run.Options(skip_embeddings=True),
            reporter=run.Reporter(quiet=True),
            downloader=FakeDownloader(source_pdf),
            git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
        )

    message = str(error.value)
    assert "two documents cannot share one requirement id" in message
    assert "SWS_Fix_00001" in message
    assert paths.extraction_report_path(manifest).is_file(), "the report must survive the failure"
    assert not paths.db_path(manifest).exists(), "nothing may be indexed after a collision"


def test_cross_document_duplicates_ignores_a_repeat_within_one_document(corpus):
    """That case is the extractor's warning; this check must not double-report it."""
    manifest, _, source_pdf = corpus
    entry = manifest.documents[0]
    pdf = paths.data_dir(manifest) / "docs" / entry.filename
    FakeDownloader(source_pdf)(entry.url, pdf)
    result = ingest_document(manifest, entry, pdf)
    result.extraction.requirements.append(result.extraction.requirements[0])

    assert run.cross_document_duplicates([result]) == []
    assert run.cross_document_duplicates([result, result]) == [], (
        "the same document object twice is one document, not a collision"
    )


def test_a_missing_key_is_reported_before_anything_is_fetched(corpus, monkeypatch, capsys):
    """The first-run path: fail in a tenth of a second, not after three minutes."""
    manifest, manifest_path, source_pdf = corpus
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr("core.openrouter.get_settings", lambda: Settings(_env_file=None))
    downloader = FakeDownloader(source_pdf)
    monkeypatch.setattr("ingestion.run.urllib_downloader", downloader)

    code = run.main([str(manifest_path), "--quiet"])

    captured = capsys.readouterr()
    assert code == 1
    assert API_KEY_ENV_VAR in captured.err
    assert "--skip-embeddings" in captured.err
    assert "Traceback" not in captured.err
    assert downloader.calls == [], "the preflight must run before the first download"


def test_a_sha_mismatch_refuses_to_index(corpus, capsys):
    manifest, manifest_path, source_pdf = corpus
    wrong = FakeGit({"communication/Fix/Fix.c": FIXTURE_C})
    wrong_sha = "f" * 40

    def runner(args, cwd):
        result = wrong(args, cwd)
        return f"{wrong_sha}\n" if args[0] == "rev-parse" else result

    with pytest.raises(Exception) as error:
        run.run_ingestion(
            manifest,
            run.Options(skip_embeddings=True),
            reporter=run.Reporter(quiet=True),
            downloader=FakeDownloader(source_pdf),
            git_runner=runner,
        )
    assert "does not match the manifest's pinned" in str(error.value)


# --------------------------------------------------------------------------
# the prototype rule
# --------------------------------------------------------------------------


def test_an_unannotated_prototype_covered_by_its_definition_is_not_embedded():
    from core.models import CodeUnit, ReqAnnotation

    def unit(kind, symbol, span, annotations=()):
        return CodeUnit(
            repo_path="a.c",
            language="c",
            symbol=symbol,
            kind=kind,
            line_span=span,
            text=f"// a.c > {symbol}",
            req_annotations=list(annotations),
            git_sha=SHA,
            project_id="fixture-can",
        )

    annotation = ReqAnnotation(
        canonical_id="SWS_Fix_00001",
        raw="@req 4.0.3/CAN00001",
        marker="@",
        claim="claimed_implemented",
        line=2,
    )
    units = [
        unit("function", "Can_Write", (10, 20)),
        unit("prototype", "Can_Write", (1, 1)),  # dropped: definition covers it
        unit("prototype", "Can_Annotated", (2, 2), [annotation]),  # kept: tier-1 evidence
        unit("prototype", "Can_HeaderOnly", (3, 3)),  # kept: no definition in scope
        unit("macro", "CAN_E_OK", (4, 4)),
    ]

    kept, dropped = run.embeddable_code_units(units)

    assert dropped == 1
    assert [u.symbol for u in kept] == [
        "Can_Write",
        "Can_Annotated",
        "Can_HeaderOnly",
        "CAN_E_OK",
    ]


# --------------------------------------------------------------------------
# the spot check's selection rules
# --------------------------------------------------------------------------


def ingestion_for(blocks: list[str], entry, manifest, *, pages: int = 1) -> DocumentIngestion:
    document = document_from_blocks(blocks, filename=entry.filename, pages=pages)
    extraction = extract_requirements(document, manifest, entry)
    chunks, stats = extract_context_chunks(document, manifest, entry, extraction)
    return DocumentIngestion(entry, extraction, chunks, stats)


def test_the_spot_check_covers_every_document_and_every_shape():
    """Real corpus, real requirement shapes, no PDF: driven off the golden pages."""
    results = [
        ingestion_for(
            [
                "[SWS_Can_00001] A titled requirement ⌈Body one.⌋()",
                "[SWS_Can_00002] ⌈Body two.⌋(SRS_Can_01001)",
                "[SWS_Can_CONSTR_00003] ⌈A constraint.⌋()",
            ],
            REAL_MANIFEST.documents[0],
            REAL_MANIFEST,
        ),
        ingestion_for(
            ["[SWS_CANIF_00010] ⌈Interface body.⌋(SRS_Can_02002)"],
            REAL_MANIFEST.documents[1],
            REAL_MANIFEST,
        ),
        # Two pages, with the body split across the break, so this document
        # supplies the bbox-is-None case.
        ingestion_for(
            ["[SWS_CanTp_00020] ⌈Transport body that", "continues after the break.⌋()"],
            REAL_MANIFEST.documents[2],
            REAL_MANIFEST,
            pages=2,
        ),
        ingestion_for(
            ["[SWS_CanSM_00030] A state manager title ⌈State body.⌋()"],
            REAL_MANIFEST.documents[3],
            REAL_MANIFEST,
        ),
    ]

    picks = select_picks(results)

    assert {pick.entry.key for pick in picks} == {
        document.key for document in REAL_MANIFEST.documents
    }, "every document must be represented"
    covered = {pick.reason for pick in picks} | {
        label for pick in picks for label in pick.also_satisfies
    }
    for label, _ in CRITERIA:
        assert label in covered, f"the sample must cover: {label}"
    assert len(picks) >= 5


def test_the_spot_check_selection_is_deterministic():
    results = [
        ingestion_for(
            [
                "[SWS_Can_00001] A titled requirement ⌈Body one.⌋()",
                "[SWS_Can_00002] ⌈Body two.⌋(SRS_Can_01001)",
            ],
            REAL_MANIFEST.documents[0],
            REAL_MANIFEST,
        )
    ]
    first = [(p.entry.key, p.requirement.id, p.reason) for p in select_picks(results)]
    second = [(p.entry.key, p.requirement.id, p.reason) for p in select_picks(results)]
    assert first == second


def test_the_rendered_spot_check_leads_with_the_filename_and_page():
    results = [
        ingestion_for(
            ["[SWS_Can_00001] A titled requirement ⌈Body one.⌋(SRS_Can_01001)"],
            REAL_MANIFEST.documents[0],
            REAL_MANIFEST,
        )
    ]
    picks = select_picks(results)
    text = render_spot_check(REAL_MANIFEST, picks)

    assert "# Spot check — WP1 gate" in text
    assert "### 📄 `AUTOSAR_CP_SWS_CANDriver.pdf` — **page 1**" in text
    assert "**upstream_ids:** `SRS_Can_01001`" in text
    assert "Body one." in text
    assert "How to verify" in text


def test_a_bbox_of_none_is_explained_rather_than_printed_as_none():
    """A page-break spanner has no bbox; the artifact must say why, not print None."""
    results = [
        ingestion_for(
            ["[SWS_Can_00001] ⌈A body that", "continues after the page break.⌋()"],
            REAL_MANIFEST.documents[0],
            REAL_MANIFEST,
            pages=2,
        )
    ]
    picks = select_picks(results)
    assert picks[0].requirement.bbox is None, "the fixture must actually span the break"
    text = render_spot_check(REAL_MANIFEST, picks)
    assert "the body spans a page break, so only `char_span` is available" in text
    assert "**bbox:** `None`" in text


# --------------------------------------------------------------------------
# the summary
# --------------------------------------------------------------------------


def test_the_summary_states_every_figure_the_gate_asks_for(corpus):
    manifest, _, source_pdf = corpus
    summary = run.run_ingestion(
        manifest,
        run.Options(),
        reporter=run.Reporter(quiet=True),
        downloader=FakeDownloader(source_pdf),
        git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
        transport=FakeTransport(),
    )
    text = run.render_summary(summary)

    assert "ingestion summary" in text
    assert "fixture_driver" in text
    assert "code units" in text
    assert "@req" in text and "!req" in text
    assert "tier-1 join" in text
    assert "sqlite rows" in text
    assert "computed" in text and "from cache" in text
    assert "reported by OpenRouter, not estimated" in text
    assert "criterion: < 5s" in text
    assert "spot check covers" in text


def test_the_cli_returns_zero_on_a_clean_run(corpus, monkeypatch, capsys):
    manifest, manifest_path, source_pdf = corpus
    monkeypatch.setattr("ingestion.run.urllib_downloader", FakeDownloader(source_pdf))
    monkeypatch.setattr(
        "ingestion.run.subprocess_git_runner", FakeGit({"communication/Fix/Fix.c": FIXTURE_C})
    )

    code = run.main([str(manifest_path), "--skip-embeddings"])

    assert code == 0
    assert "ingestion summary" in capsys.readouterr().out


def test_the_run_records_each_documents_page_count(corpus):
    """`RequirementCitation.page_count` (spec §6) has no other source.

    It is measurable only while the PDF is open, and a citation needs it long
    after ingestion has finished — possibly after the gitignored PDFs are gone.
    """
    manifest, _, source_pdf = corpus

    run.run_ingestion(
        manifest,
        run.Options(),
        reporter=run.Reporter(quiet=True),
        downloader=FakeDownloader(source_pdf),
        git_runner=FakeGit({"communication/Fix/Fix.c": FIXTURE_C}),
        transport=FakeTransport(),
    )

    conn = db.connect(paths.db_path(manifest))
    try:
        recorded = db.page_counts(conn, manifest.project_id)
        assert set(recorded) == {entry.key for entry in manifest.documents}
        assert all(count > 0 for count in recorded.values())
    finally:
        conn.close()
