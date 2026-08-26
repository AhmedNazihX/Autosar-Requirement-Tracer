"""Tests for ingestion/fetcher.py (S1.3.1).

The acceptance criterion is "re-run is a no-op", so the network call is
always a fake here and the tests assert on how many times it was invoked —
a second run that reports ``skipped`` while having called the downloader
again would pass a naive assertion on the action alone.
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core.manifest import DocumentEntry, load_manifest
from ingestion import fetcher
from ingestion.fetcher import (
    PINNED_INTERMEDIATE_ISSUER_CN,
    PINNED_INTERMEDIATE_PEM,
    PINNED_INTERMEDIATE_SHA256,
    PINNED_INTERMEDIATE_SUBJECT_CN,
    FetchError,
    corpus_ssl_context,
    docs_dir,
    fetch_document,
    fetch_documents,
    fingerprint,
    read_pinned_intermediate,
    read_sidecar,
    sha256_file,
    sidecar_path,
    urllib_downloader,
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


# --------------------------------------------------------------------------
# Content-Length is a contract, not decoration
# --------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for the object ``urlopen`` returns."""

    def __init__(self, body: bytes, declared_length: int | None) -> None:
        self._body = body
        self._offset = 0
        headers = {} if declared_length is None else {"Content-Length": str(declared_length)}
        self.headers = headers

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._body) - self._offset
        chunk = self._body[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def fake_urlopen(response: FakeResponse):
    def _open(request, timeout=None, context=None):  # noqa: ANN001 - urlopen shape
        return response

    return _open


def test_short_read_against_content_length_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A clean close mid-body must not install a truncated PDF.

    The truncated bytes still start with '%PDF', so without the byte-count
    check the file would be installed, sidecar'd against its own truncated
    contents, and reported 'skipped' for ever after.
    """
    truncated = PDF_BODY[: len(PDF_BODY) // 2]
    monkeypatch.setattr(
        urllib.request, "urlopen", fake_urlopen(FakeResponse(truncated, len(PDF_BODY)))
    )
    dest = tmp_path / "doc.pdf.part"

    with pytest.raises(FetchError, match="truncated download"):
        urllib_downloader("https://example.invalid/doc.pdf", dest)


def test_truncated_download_is_never_installed_as_a_pdf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    truncated = PDF_BODY[:64]
    assert truncated.startswith(b"%PDF"), "the whole point: magic bytes still look fine"
    monkeypatch.setattr(
        urllib.request, "urlopen", fake_urlopen(FakeResponse(truncated, len(PDF_BODY)))
    )
    document = make_document()

    with pytest.raises(FetchError, match="truncated download"):
        fetch_document(document, tmp_path, downloader=urllib_downloader)

    assert not (tmp_path / document.filename).exists()
    assert not sidecar_path(tmp_path / document.filename).exists()


def test_complete_read_matching_content_length_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(
        urllib.request, "urlopen", fake_urlopen(FakeResponse(PDF_BODY, len(PDF_BODY)))
    )
    written = urllib_downloader("https://example.invalid/doc.pdf", tmp_path / "doc.part")
    assert written == len(PDF_BODY)


def test_absent_content_length_is_tolerated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """No Content-Length means no cross-check is possible — not a failure."""
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen(FakeResponse(PDF_BODY, None)))
    written = urllib_downloader("https://example.invalid/doc.pdf", tmp_path / "doc.part")
    assert written == len(PDF_BODY)


# --------------------------------------------------------------------------
# the pinned intermediate certificate
#
# The corpus host omits the intermediate that signed its leaf, so a
# certificate is pinned in the repo to complete the chain. Nothing fetched at
# runtime is ever trusted. These assertions are all offline.
# --------------------------------------------------------------------------


def test_pinned_certificate_matches_its_fingerprint():
    """The pin itself: swapping the PEM for any other certificate fails here.

    The expected digest is independently justified — the certificate's
    signature chains to "Starfield Root Certificate Authority - G2", which
    already ships in certifi, so a forgery could not satisfy it:
        openssl verify -CAfile "$(python -c 'import certifi;print(certifi.where())')" \
            ingestion/certs/starfield-secure-ca-g2.pem   ->  OK
    """
    block = read_pinned_intermediate()
    digest = fingerprint(ssl.PEM_cert_to_DER_cert(block))
    assert digest == PINNED_INTERMEDIATE_SHA256, (
        f"{PINNED_INTERMEDIATE_PEM} is not the pinned certificate: expected SHA-256 "
        f"{PINNED_INTERMEDIATE_SHA256}, got {digest}"
    )


def test_pinned_certificate_is_the_expected_ca():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=read_pinned_intermediate())
    certs = context.get_ca_certs()
    assert len(certs) == 1, "the pinned file must hold exactly one certificate"

    def common_name(field: str) -> str | None:
        return dict(pair for rdn in certs[0][field] for pair in rdn).get("commonName")

    assert common_name("subject") == PINNED_INTERMEDIATE_SUBJECT_CN
    assert common_name("issuer") == PINNED_INTERMEDIATE_ISSUER_CN, (
        "the issuer must be the root that certifi already trusts, or the chain "
        "cannot complete"
    )


def test_pinned_certificate_is_within_its_validity_window():
    """Fails in CI before it fails in production. The pin expires 2031-05-03."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=read_pinned_intermediate())
    cert = context.get_ca_certs()[0]
    not_before = ssl.cert_time_to_seconds(cert["notBefore"])
    not_after = ssl.cert_time_to_seconds(cert["notAfter"])
    now = time.time()

    assert not_before <= now, f"pinned certificate is not valid yet ({cert['notBefore']})"
    assert now < not_after, (
        f"THE PINNED CERTIFICATE HAS EXPIRED ({cert['notAfter']}). Corpus downloads will "
        f"fail. Replace {PINNED_INTERMEDIATE_PEM} with the current "
        f"{PINNED_INTERMEDIATE_SUBJECT_CN!r} certificate and update "
        "PINNED_INTERMEDIATE_SHA256."
    )


def test_corpus_ssl_context_verifies_fully():
    context = corpus_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    subjects = {
        dict(pair for rdn in cert["subject"] for pair in rdn).get("commonName")
        for cert in context.get_ca_certs()
    }
    assert PINNED_INTERMEDIATE_SUBJECT_CN in subjects
    assert PINNED_INTERMEDIATE_ISSUER_CN in subjects, "certifi should already carry the root"


def test_missing_pinned_certificate_fails_with_an_actionable_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    monkeypatch.setattr(fetcher, "PINNED_INTERMEDIATE_PEM", tmp_path / "absent.pem")
    with pytest.raises(FetchError, match="pinned intermediate certificate missing") as caught:
        read_pinned_intermediate()
    assert PINNED_INTERMEDIATE_SHA256 in str(caught.value)


def test_a_substituted_certificate_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """The pin must reject a different real certificate, not just garbage."""
    import certifi

    bundle = Path(certifi.where()).read_text(encoding="utf-8")
    begin, end = "-----BEGIN CERTIFICATE-----", "-----END CERTIFICATE-----"
    first = bundle[bundle.index(begin) : bundle.index(end) + len(end)] + "\n"
    substitute = tmp_path / "substitute.pem"
    substitute.write_text(first, encoding="utf-8")
    monkeypatch.setattr(fetcher, "PINNED_INTERMEDIATE_PEM", substitute)

    with pytest.raises(FetchError, match="certificate pin mismatch") as caught:
        read_pinned_intermediate()
    assert PINNED_INTERMEDIATE_SHA256 in str(caught.value)


def test_a_non_pem_pinned_file_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    bad = tmp_path / "bad.pem"
    bad.write_text("# just a comment, no certificate\n", encoding="utf-8")
    monkeypatch.setattr(fetcher, "PINNED_INTERMEDIATE_PEM", bad)
    with pytest.raises(FetchError, match="no PEM certificate block found"):
        read_pinned_intermediate()


def test_certificate_verification_failure_points_at_the_pinned_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    verification_error = ssl.SSLCertVerificationError("certificate verify failed")
    verification_error.verify_code = 20

    def raising_urlopen(request, timeout=None, context=None):  # noqa: ANN001
        raise urllib.error.URLError(verification_error)

    monkeypatch.setattr(urllib.request, "urlopen", raising_urlopen)
    with pytest.raises(FetchError, match="TLS certificate verification failed") as caught:
        urllib_downloader("https://example.invalid/doc.pdf", tmp_path / "doc.part")
    message = str(caught.value)
    assert str(PINNED_INTERMEDIATE_PEM) in message
    assert PINNED_INTERMEDIATE_SHA256 in message
