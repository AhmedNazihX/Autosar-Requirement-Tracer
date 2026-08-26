"""Tests for ingestion/fetcher.py (S1.3.1).

The acceptance criterion is "re-run is a no-op", so the network call is
always a fake here and the tests assert on how many times it was invoked —
a second run that reports ``skipped`` while having called the downloader
again would pass a naive assertion on the action alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.manifest import DocumentEntry, load_manifest
from ingestion.fetcher import (
    FetchError,
    docs_dir,
    fetch_document,
    fetch_documents,
    read_sidecar,
    sha256_file,
    sidecar_path,
    write_sidecar,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MANIFEST_PATH = REPO_ROOT / "projects" / "autosar-can" / "project.yaml"

PDF_BODY = b"%PDF-1.7\n%%fake body for tests\n" + b"x" * 4096
OTHER_PDF_BODY = b"%PDF-1.7\n%%a different fake body\n" + b"y" * 2048
HTML_BODY = b"<!doctype html>\n<html><body>Sign in to the guest network</body></html>\n"


def make_document(**overrides: object) -> DocumentEntry:
    data: dict[str, object] = {
        "key": "can_driver",
        "title": "Specification of CAN Driver",
        "url": "https://example.invalid/AUTOSAR_CP_SWS_CANDriver.pdf",
        "filename": "AUTOSAR_CP_SWS_CANDriver.pdf",
        "module": "Can",
        "req_id_pattern": r"SWS_Can(?:_CONSTR)?_\d+",
        "expected_requirements": 240,
    }
    data.update(overrides)
    return DocumentEntry.model_validate(data)


class RecordingDownloader:
    """Fake downloader that records every call and writes a canned body."""

    def __init__(self, body: bytes = PDF_BODY, *, fail_after: int | None = None) -> None:
        self.body = body
        self.fail_after = fail_after
        self.calls: list[str] = []

    def __call__(self, url, dest, on_chunk=None):  # noqa: ANN001 - matches Downloader
        self.calls.append(url)
        if self.fail_after is not None:
            dest.write_bytes(self.body[: self.fail_after])
            if on_chunk is not None:
                on_chunk(self.fail_after, len(self.body))
            raise FetchError(f"{url}: simulated connection reset mid-download")
        dest.write_bytes(self.body)
        if on_chunk is not None:
            on_chunk(len(self.body), len(self.body))
        return len(self.body)


# --------------------------------------------------------------------------
# S1.3.1 acceptance: run 1 downloads, run 2 performs zero network requests
# --------------------------------------------------------------------------


def test_first_run_downloads_and_writes_sidecar(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader()

    result = fetch_document(document, tmp_path, downloader=downloader)

    assert result.action == "downloaded"
    assert downloader.calls == [document.url]
    assert result.path == tmp_path / document.filename
    assert result.path.read_bytes() == PDF_BODY
    assert result.bytes == len(PDF_BODY)
    assert result.sha256 == sha256_file(result.path)
    assert read_sidecar(result.path) == result.sha256


def test_second_run_is_a_no_op_with_zero_network_calls(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader()

    first = fetch_document(document, tmp_path, downloader=downloader)
    assert downloader.calls == [document.url]

    second = fetch_document(document, tmp_path, downloader=downloader)

    # The whole point: no second call, not merely a "skipped" label.
    assert downloader.calls == [document.url]
    assert second.action == "skipped"
    assert second.sha256 == first.sha256
    assert second.bytes == first.bytes


def test_whole_manifest_second_run_skips_every_document(tmp_path: Path):
    manifest = load_manifest(REAL_MANIFEST_PATH)
    downloader = RecordingDownloader()

    first = fetch_documents(manifest, tmp_path, downloader=downloader)
    assert [r.action for r in first] == ["downloaded"] * len(manifest.documents)
    assert len(downloader.calls) == len(manifest.documents)
    assert [r.key for r in first] == [d.key for d in manifest.documents]

    second = fetch_documents(manifest, tmp_path, downloader=downloader)
    assert [r.action for r in second] == ["skipped"] * len(manifest.documents)
    assert len(downloader.calls) == len(manifest.documents)


# --------------------------------------------------------------------------
# cache invalidation
# --------------------------------------------------------------------------


def test_changed_digest_triggers_redownload(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader()
    result = fetch_document(document, tmp_path, downloader=downloader)

    # Someone truncated or replaced the file without touching the sidecar.
    result.path.write_bytes(OTHER_PDF_BODY)

    again = fetch_document(document, tmp_path, downloader=downloader)
    assert again.action == "redownloaded"
    assert len(downloader.calls) == 2
    assert again.path.read_bytes() == PDF_BODY
    assert read_sidecar(again.path) == again.sha256


def test_corrupted_sidecar_triggers_redownload(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader()
    result = fetch_document(document, tmp_path, downloader=downloader)

    sidecar_path(result.path).write_text("not a digest\n", encoding="utf-8")
    assert read_sidecar(result.path) is None

    again = fetch_document(document, tmp_path, downloader=downloader)
    assert again.action == "redownloaded"
    assert len(downloader.calls) == 2
    assert read_sidecar(again.path) == again.sha256


def test_missing_sidecar_triggers_redownload(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader()
    result = fetch_document(document, tmp_path, downloader=downloader)
    sidecar_path(result.path).unlink()

    again = fetch_document(document, tmp_path, downloader=downloader)
    assert again.action == "redownloaded"
    assert len(downloader.calls) == 2


def test_force_redownloads_an_intact_file(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader()
    fetch_document(document, tmp_path, downloader=downloader)

    again = fetch_document(document, tmp_path, downloader=downloader, force=True)
    assert again.action == "redownloaded"
    assert len(downloader.calls) == 2


# --------------------------------------------------------------------------
# a 200 response is not proof of a PDF
# --------------------------------------------------------------------------


def test_non_pdf_body_raises_and_installs_nothing(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader(body=HTML_BODY)

    with pytest.raises(FetchError, match="is not a PDF"):
        fetch_document(document, tmp_path, downloader=downloader)

    target = tmp_path / document.filename
    assert not target.exists()
    assert not sidecar_path(target).exists()
    assert (tmp_path / f"{document.filename}.part").exists()


def test_empty_body_raises(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader(body=b"")

    with pytest.raises(FetchError, match="0 bytes"):
        fetch_document(document, tmp_path, downloader=downloader)
    assert not (tmp_path / document.filename).exists()


def test_interrupted_download_leaves_no_pdf(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader(fail_after=16)

    with pytest.raises(FetchError, match="simulated connection reset"):
        fetch_document(document, tmp_path, downloader=downloader)

    target = tmp_path / document.filename
    part = tmp_path / f"{document.filename}.part"
    assert not target.exists(), "a truncated download must never be installed as the .pdf"
    assert not sidecar_path(target).exists()
    assert part.exists() and part.stat().st_size == 16

    # And a retry still recovers, rather than trusting the leftover .part.
    good = RecordingDownloader()
    result = fetch_document(document, tmp_path, downloader=good)
    assert result.action == "downloaded"
    assert result.path.read_bytes() == PDF_BODY
    assert not part.exists()


# --------------------------------------------------------------------------
# progress hook (wired now for the SSE story S3.6.2)
# --------------------------------------------------------------------------


def test_on_progress_receives_filename_and_byte_counts(tmp_path: Path):
    document = make_document()
    seen: list[tuple[str, int, int | None]] = []

    fetch_document(
        document,
        tmp_path,
        downloader=RecordingDownloader(),
        on_progress=lambda name, done, total: seen.append((name, done, total)),
    )

    assert seen == [(document.filename, len(PDF_BODY), len(PDF_BODY))]


def test_on_progress_is_not_called_when_skipping(tmp_path: Path):
    document = make_document()
    downloader = RecordingDownloader()
    fetch_document(document, tmp_path, downloader=downloader)

    seen: list[tuple[str, int, int | None]] = []
    result = fetch_document(
        document,
        tmp_path,
        downloader=downloader,
        on_progress=lambda name, done, total: seen.append((name, done, total)),
    )
    assert result.action == "skipped"
    assert seen == []


# --------------------------------------------------------------------------
# sidecar + path helpers
# --------------------------------------------------------------------------


def test_sidecar_round_trip(tmp_path: Path):
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(PDF_BODY)
    digest = sha256_file(pdf)

    written = write_sidecar(pdf, digest)
    assert written.name == "doc.pdf.sha256"
    # sha256sum format, so `shasum -a 256 -c` can verify it by hand.
    assert written.read_text(encoding="utf-8") == f"{digest}  doc.pdf\n"
    assert read_sidecar(pdf) == digest


def test_read_sidecar_missing_returns_none(tmp_path: Path):
    assert read_sidecar(tmp_path / "absent.pdf") is None


def test_docs_dir_is_repo_data_docs():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    assert docs_dir(manifest) == REPO_ROOT / "data" / "docs"


def test_docs_dir_without_project_root_raises():
    manifest = load_manifest(REAL_MANIFEST_PATH)
    manifest.manifest_dir = None
    with pytest.raises(FetchError, match="project_root"):
        docs_dir(manifest)
