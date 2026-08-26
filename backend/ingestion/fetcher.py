"""Fetch the corpus PDFs named by a project manifest into ``data/docs/``.

Story S1.3.1. The URLs and filenames are *never* hard-coded here — they come
from ``manifest.documents`` (see ``core/manifest.py``), which is the corpus
abstraction boundary the design spec (§2) locks down. Iterating over the
manifest is what lets the corpus grow (it went from two to four documents)
without touching this module.

Behaviour that matters downstream:

* **Skip-if-present.** Each download writes a ``<filename>.sha256`` sidecar.
  A later run that finds the file with a matching digest performs *zero*
  network requests and reports ``skipped``. A missing, unparseable or
  mismatched sidecar (or a truncated file) yields ``redownloaded``.
* **Atomic writes.** Bytes stream into ``<filename>.part`` and are renamed
  into place only after the byte count matches ``Content-Length`` and the
  ``%PDF`` magic check passes, so an interrupted, truncated or hijacked run
  can never leave a file that a later existence check would accept. A failed
  attempt leaves the ``.part`` behind for inspection; a ``.part`` is never
  promoted to ``.pdf``.
* **Progress hook.** ``on_progress(filename, bytes_done, bytes_total)`` is
  called as bytes arrive (``bytes_total`` is ``None`` when the server sends
  no ``Content-Length``). Story S3.6.2 streams these over SSE; nothing here
  knows about SSE.
* **TLS.** The corpus host serves an incomplete certificate chain, completed
  by a certificate pinned in ``ingestion/certs/`` — see the block comment
  above :data:`PINNED_INTERMEDIATE_PEM`. Verification is never relaxed and
  nothing fetched at runtime is ever trusted.

The network call is isolated behind the ``downloader`` parameter so tests can
substitute a fake and *prove* the second run never calls it.
"""

from __future__ import annotations

import hashlib
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import certifi

from core.manifest import DocumentEntry, ProjectManifest

#: ``(filename, bytes_done, bytes_total_or_None)``.
ProgressCallback = Callable[[str, int, int | None], None]

#: ``(bytes_done, bytes_total_or_None)`` — the per-download view handed to a
#: :data:`Downloader`, already bound to the document's final filename.
ChunkCallback = Callable[[int, int | None], None]

#: ``(url, destination_part_file, chunk_callback) -> bytes_written``.
Downloader = Callable[[str, Path, ChunkCallback | None], int]

FetchAction = Literal["downloaded", "skipped", "redownloaded"]

PDF_MAGIC = b"%PDF"
_CHUNK_BYTES = 256 * 1024
_TIMEOUT_SECONDS = 120
# autosar.org serves the standards to ordinary browsers; a default urllib
# User-Agent is a needless way to get a 403.
_USER_AGENT = "ReqTrace/0.1 (+https://github.com/openAUTOSAR/classic-platform)"

# --- the pinned intermediate certificate -----------------------------------
#
# www.autosar.org presents ONLY its leaf certificate and omits the
# "Starfield Secure Certificate Authority - G2" intermediate that signed it
# (``openssl s_client -showcerts`` returns one certificate and
# "Verify return code: 21"). Browsers recover by fetching the missing issuer
# from the leaf's AIA extension; OpenSSL, and therefore Python, does not, so
# verification fails with "unable to get local issuer certificate".
#
# The fix is a certificate pinned IN THE REPOSITORY, never one fetched at
# runtime. Fetching the issuer named by an unverified leaf and installing it
# would let an on-path attacker nominate their own trust anchor; a pinned
# file cannot be influenced by the network at all. The pin is enforced on
# every use (:func:`read_pinned_intermediate`), so swapping the file for
# another certificate fails loudly instead of silently widening trust.
#
# The certificate is genuine independently of how its bytes reached this
# repository: its signature chains to "Starfield Root Certificate Authority
# - G2", which already ships in certifi, and a forgery could not satisfy
# that signature check.
#
#   openssl verify -CAfile "$(python -c 'import certifi;print(certifi.where())')" \
#       ingestion/certs/starfield-secure-ca-g2.pem                        -> OK
#
CERTS_DIR = Path(__file__).resolve().parent / "certs"
PINNED_INTERMEDIATE_PEM = CERTS_DIR / "starfield-secure-ca-g2.pem"
PINNED_INTERMEDIATE_SHA256 = "93a07898d89b2cca166ba6f1f8a14138ce43828e491b831926bc8247d391cc72"
PINNED_INTERMEDIATE_SUBJECT_CN = "Starfield Secure Certificate Authority - G2"
PINNED_INTERMEDIATE_ISSUER_CN = "Starfield Root Certificate Authority - G2"

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


class FetchError(Exception):
    """Raised when a document cannot be fetched or is not a PDF."""


def fingerprint(der: bytes) -> str:
    """Lower-case hex SHA-256 of a DER-encoded certificate."""
    return hashlib.sha256(der).hexdigest()


def read_pinned_intermediate() -> str:
    """Return the pinned intermediate as a PEM block, enforcing its SHA-256 pin.

    Raises :class:`FetchError` — naming the file and both fingerprints — if
    the file is absent, is not a single PEM certificate, or does not match
    :data:`PINNED_INTERMEDIATE_SHA256`. There is deliberately no fallback:
    the alternative to a verified pin is not "try something else", it is
    "stop".
    """
    if not PINNED_INTERMEDIATE_PEM.is_file():
        raise FetchError(
            f"pinned intermediate certificate missing: {PINNED_INTERMEDIATE_PEM}. "
            f"It must be {PINNED_INTERMEDIATE_SUBJECT_CN!r} with SHA-256 "
            f"{PINNED_INTERMEDIATE_SHA256}. Without it the corpus host's incomplete "
            "certificate chain cannot be completed and no document can be fetched."
        )

    text = PINNED_INTERMEDIATE_PEM.read_text(encoding="utf-8")
    start = text.find(_PEM_BEGIN)
    end = text.find(_PEM_END)
    if start == -1 or end == -1:
        raise FetchError(
            f"{PINNED_INTERMEDIATE_PEM}: no PEM certificate block found "
            f"(expected {_PEM_BEGIN!r})"
        )
    block = text[start : end + len(_PEM_END)] + "\n"

    try:
        der = ssl.PEM_cert_to_DER_cert(block)
    except (ValueError, TypeError) as exc:
        raise FetchError(f"{PINNED_INTERMEDIATE_PEM}: not a valid PEM certificate — {exc}") from exc

    actual = fingerprint(der)
    if actual != PINNED_INTERMEDIATE_SHA256:
        raise FetchError(
            f"{PINNED_INTERMEDIATE_PEM}: certificate pin mismatch — refusing to trust it. "
            f"Expected SHA-256 {PINNED_INTERMEDIATE_SHA256}, got {actual}. "
            f"The pinned file must be {PINNED_INTERMEDIATE_SUBJECT_CN!r}, issued by "
            f"{PINNED_INTERMEDIATE_ISSUER_CN!r}."
        )
    return block


@lru_cache(maxsize=1)
def corpus_ssl_context() -> ssl.SSLContext:
    """A fully verifying SSL context that can complete the corpus host's chain.

    ``certifi`` supplies the roots (deterministic across platforms, rather
    than whatever the host OS happens to ship) and the pinned intermediate is
    added so the corpus host's truncated chain reaches one of them. Hostname
    checking and ``CERT_REQUIRED`` are left exactly as
    :func:`ssl.create_default_context` sets them — this context verifies no
    less than the default one, it simply knows one more real CA certificate
    whose own root is already trusted.
    """
    block = read_pinned_intermediate()
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        # cadata, not cafile: what gets installed is exactly the bytes whose
        # fingerprint was just checked, with no second read of the file.
        context.load_verify_locations(cadata=block)
    except ssl.SSLError as exc:
        raise FetchError(
            f"{PINNED_INTERMEDIATE_PEM}: OpenSSL rejected the pinned certificate — {exc}"
        ) from exc
    return context


@dataclass(frozen=True)
class FetchResult:
    """Outcome of fetching one manifest document.

    ``action`` is the assertable part: a second run over an intact
    ``data/docs/`` must report ``skipped`` for every document.
    """

    key: str
    filename: str
    action: FetchAction
    sha256: str
    bytes: int
    path: Path


def docs_dir(manifest: ProjectManifest) -> Path:
    """Return the ``data/docs/`` directory for ``manifest``.

    Resolved against the repo root that owns the manifest, not the process
    CWD, so ``uv run`` from ``backend/`` and from the repo root agree.
    """
    root = manifest.project_root
    if root is None:
        raise FetchError(
            "manifest has no project_root (was it constructed directly instead of "
            "via load_manifest?) — pass dest_dir explicitly"
        )
    return root / "data" / "docs"


def sha256_file(path: Path) -> str:
    """SHA-256 of ``path``, read in chunks (the PDFs are megabytes)."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def sidecar_path(pdf_path: Path) -> Path:
    """The ``<filename>.sha256`` sidecar path for ``pdf_path``."""
    return pdf_path.with_name(pdf_path.name + ".sha256")


def read_sidecar(pdf_path: Path) -> str | None:
    """Return the digest recorded in the sidecar, or ``None``.

    ``None`` covers "absent" and "corrupted" alike — both mean the cached
    file cannot be trusted, so the caller re-downloads. The format is the
    usual ``sha256sum`` one: ``<64 hex>  <filename>``.
    """
    path = sidecar_path(pdf_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    parts = raw.split()
    if not parts:
        return None
    digest = parts[0].lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        return None
    return digest


def write_sidecar(pdf_path: Path, digest: str) -> Path:
    """Write the ``sha256sum``-format sidecar for ``pdf_path``."""
    path = sidecar_path(pdf_path)
    path.write_text(f"{digest}  {pdf_path.name}\n", encoding="utf-8")
    return path


def _stream(
    url: str,
    dest: Path,
    on_chunk: ChunkCallback | None,
    context: ssl.SSLContext | None,
) -> int:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    written = 0
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS, context=context) as response:
        raw_length = response.headers.get("Content-Length")
        total = int(raw_length) if raw_length and raw_length.isdigit() else None
        with dest.open("wb") as fh:
            while chunk := response.read(_CHUNK_BYTES):
                fh.write(chunk)
                written += len(chunk)
                if on_chunk is not None:
                    on_chunk(written, total)

    # A connection that closes cleanly mid-body raises nothing, and the first
    # four bytes of a truncated PDF are still "%PDF" — so without this check a
    # short read would be installed as a complete document and then reported
    # "skipped" for ever, its sidecar matching the truncated bytes.
    if total is not None and written != total:
        raise FetchError(
            f"{url}: truncated download — got {written:,} of {total:,} bytes "
            "declared by Content-Length"
        )
    return written


def urllib_downloader(
    url: str,
    dest: Path,
    on_chunk: ChunkCallback | None = None,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> int:
    """Stream ``url`` into ``dest``, reporting progress. Returns bytes written.

    Uses ``urllib`` from the stdlib deliberately: this is a one-shot blocking
    download run from a CLI, so it needs no HTTP client dependency.

    TLS verification uses :func:`corpus_ssl_context` — certifi's roots plus
    the pinned intermediate the corpus host omits — unless the caller passes
    its own ``ssl_context``. There is no fallback and no retry: if
    verification fails, it fails.
    """
    context = ssl_context if ssl_context is not None else corpus_ssl_context()
    try:
        return _stream(url, dest, on_chunk, context)
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise FetchError(
                f"{url}: TLS certificate verification failed — {exc.reason}. "
                f"The corpus host omits an intermediate certificate, which is supplied by "
                f"{PINNED_INTERMEDIATE_PEM}; check that file is present and unmodified "
                f"(expected SHA-256 {PINNED_INTERMEDIATE_SHA256})."
            ) from exc
        raise FetchError(f"{url}: network error — {exc.reason}") from exc


def _looks_like_pdf(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(len(PDF_MAGIC)) == PDF_MAGIC


def _bind_progress(on_progress: ProgressCallback | None, filename: str) -> ChunkCallback | None:
    """Adapt the public ``(filename, done, total)`` hook to a downloader's hook."""
    if on_progress is None:
        return None

    def on_chunk(done: int, total: int | None) -> None:
        on_progress(filename, done, total)

    return on_chunk


def fetch_document(
    document: DocumentEntry,
    dest_dir: Path,
    *,
    downloader: Downloader = urllib_downloader,
    on_progress: ProgressCallback | None = None,
    force: bool = False,
) -> FetchResult:
    """Ensure ``document`` is present under ``dest_dir``; return what happened.

    Performs no network request at all when the file is present and its digest
    matches the sidecar (unless ``force``).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / document.filename

    if not force and target.is_file():
        recorded = read_sidecar(target)
        if recorded is not None:
            actual = sha256_file(target)
            if actual == recorded:
                return FetchResult(
                    key=document.key,
                    filename=document.filename,
                    action="skipped",
                    sha256=actual,
                    bytes=target.stat().st_size,
                    path=target,
                )
        action: FetchAction = "redownloaded"
    else:
        action = "redownloaded" if target.is_file() else "downloaded"

    part = target.with_name(target.name + ".part")
    written = downloader(document.url, part, _bind_progress(on_progress, document.filename))

    if not part.is_file():
        raise FetchError(f"{document.filename}: downloader wrote no file to {part}")
    if written == 0 or part.stat().st_size == 0:
        raise FetchError(f"{document.filename}: downloaded 0 bytes from {document.url}")
    if not _looks_like_pdf(part):
        raise FetchError(
            f"{document.filename}: response from {document.url} is not a PDF "
            f"(missing {PDF_MAGIC.decode()} magic bytes) — refusing to install it; "
            f"the partial download was left at {part}"
        )

    digest = sha256_file(part)
    part.replace(target)
    write_sidecar(target, digest)

    return FetchResult(
        key=document.key,
        filename=document.filename,
        action=action,
        sha256=digest,
        bytes=target.stat().st_size,
        path=target,
    )


def fetch_documents(
    manifest: ProjectManifest,
    dest_dir: Path | None = None,
    *,
    downloader: Downloader = urllib_downloader,
    on_progress: ProgressCallback | None = None,
    force: bool = False,
) -> list[FetchResult]:
    """Fetch every document the manifest lists, in manifest order."""
    target_dir = dest_dir if dest_dir is not None else docs_dir(manifest)
    return [
        fetch_document(
            document,
            target_dir,
            downloader=downloader,
            on_progress=on_progress,
            force=force,
        )
        for document in manifest.documents
    ]


def main(argv: list[str] | None = None) -> int:
    """CLI: ``uv run python -m ingestion.fetcher <manifest.yaml> [--force]``."""
    import argparse

    from core.manifest import load_manifest

    parser = argparse.ArgumentParser(description="Fetch corpus PDFs named by a project manifest.")
    parser.add_argument("manifest", help="path to projects/<name>/project.yaml")
    parser.add_argument("--dest", default=None, help="override the destination directory")
    parser.add_argument("--force", action="store_true", help="re-download even if present")
    parser.add_argument("--quiet", action="store_true", help="suppress progress lines")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    dest = Path(args.dest).resolve() if args.dest else docs_dir(manifest)
    print(f"destination: {dest}")

    seen: set[str] = set()

    def progress(filename: str, done: int, total: int | None) -> None:
        if args.quiet:
            return
        if filename not in seen:
            seen.add(filename)
            print(f"  downloading {filename} ...")

    results = fetch_documents(manifest, dest, on_progress=progress, force=args.force)
    for result in results:
        print(
            f"{result.action:<13} {result.filename:<44} "
            f"{result.bytes:>9,} bytes  sha256={result.sha256[:16]}..."
        )
    downloads = sum(1 for r in results if r.action != "skipped")
    print(f"{len(results)} document(s): {downloads} fetched, {len(results) - downloads} skipped")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
