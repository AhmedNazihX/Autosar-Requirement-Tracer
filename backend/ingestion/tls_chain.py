"""Complete an incomplete TLS certificate chain via the leaf's AIA extension.

Why this exists: ``www.autosar.org`` — the host serving the corpus PDFs —
presents **only its leaf certificate** and omits the "Starfield Secure
Certificate Authority - G2" intermediate. Browsers and macOS ``curl`` still
succeed because they fetch the missing issuer from the leaf's Authority
Information Access (AIA) extension; OpenSSL, which is what Python's ``ssl``
uses, does not do that and fails with ``CERTIFICATE_VERIFY_FAILED: unable to
get local issuer certificate``. Verified with
``openssl s_client -showcerts``: the server sends one certificate and
``Verify return code: 21``.

So the fetcher retries once with a context that has been handed the
intermediate the server forgot. This does **not** weaken verification:

* hostname checking and full chain verification stay on;
* the fetched intermediate must itself chain up to a root already trusted by
  the default store, so an attacker who injects a bogus intermediate gains
  nothing;
* the AIA URI is read from the certificate the server itself presented, and
  is fetched over plain HTTP exactly as the AIA specification intends
  (the content is a certificate, which is self-authenticating).

Implementation note: the stdlib has no ASN.1 parser, and this project may not
add one. ``SSLContext.get_ca_certs()`` only reports certificates with
``basicConstraints: CA:TRUE``, so it cannot parse a leaf. What is done
instead is a bounded scan of the leaf DER for the single well-known DER TLV
of the ``id-ad-caIssuers`` OID followed by a ``uniformResourceIdentifier``
``GeneralName`` — a fixed 10-byte needle plus a length-prefixed string, not a
general-purpose parser.
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from functools import lru_cache
from urllib.parse import urlsplit

#: DER TLV for OBJECT IDENTIFIER 1.3.6.1.5.5.7.48.2 (``id-ad-caIssuers``),
#: i.e. ``06 08 2B 06 01 05 05 07 30 02``.
_CA_ISSUERS_OID_TLV = bytes.fromhex("06082b06010505073002")

#: ``GeneralName`` choice [6] ``uniformResourceIdentifier``, context-specific
#: primitive, short-form length.
_URI_GENERAL_NAME_TAG = 0x86

#: OpenSSL X509_V_ERR codes that mean "the chain the peer sent is incomplete".
INCOMPLETE_CHAIN_VERIFY_CODES = frozenset({20, 21})

_PROBE_TIMEOUT_SECONDS = 30


class ChainCompletionError(Exception):
    """Raised when the missing intermediate cannot be located or fetched."""


def is_incomplete_chain_error(error: BaseException) -> bool:
    """True if ``error`` is a verification failure caused by a missing issuer.

    Accepts the raw :class:`ssl.SSLCertVerificationError` or the
    :class:`urllib.error.URLError` that urllib wraps it in.
    """
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        return isinstance(reason, BaseException) and is_incomplete_chain_error(reason)
    return (
        isinstance(error, ssl.SSLCertVerificationError)
        and error.verify_code in INCOMPLETE_CHAIN_VERIFY_CODES
    )


def ca_issuers_uris(leaf_der: bytes) -> list[str]:
    """Extract the AIA ``caIssuers`` HTTP URIs from a DER-encoded certificate.

    Returns an empty list when the certificate carries no such extension.
    """
    uris: list[str] = []
    index = leaf_der.find(_CA_ISSUERS_OID_TLV)
    while index != -1:
        cursor = index + len(_CA_ISSUERS_OID_TLV)
        if (
            cursor + 1 < len(leaf_der)
            and leaf_der[cursor] == _URI_GENERAL_NAME_TAG
            and leaf_der[cursor + 1] < 0x80  # short-form length only
        ):
            length = leaf_der[cursor + 1]
            raw = leaf_der[cursor + 2 : cursor + 2 + length]
            try:
                uri = raw.decode("ascii")
            except UnicodeDecodeError:
                uri = ""
            if uri.startswith("http://") or uri.startswith("https://"):
                uris.append(uri)
        index = leaf_der.find(_CA_ISSUERS_OID_TLV, cursor)
    return uris


def fetch_leaf_der(host: str, port: int = 443) -> bytes:
    """Return the DER leaf certificate ``host`` presents.

    Verification is deliberately off for this probe: the point is to read the
    certificate that failed to verify. Nothing is trusted as a result of it —
    the certificate is only mined for its AIA URI, and the real download that
    follows verifies the full chain.
    """
    probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    probe.check_hostname = False
    probe.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_SECONDS) as sock:
            with probe.wrap_socket(sock, server_hostname=host) as tls:
                der = tls.getpeercert(binary_form=True)
    except OSError as exc:
        raise ChainCompletionError(
            f"{host}:{port}: could not read the server certificate — {exc}"
        ) from exc
    if not der:
        raise ChainCompletionError(f"{host}:{port}: server presented no certificate")
    return der


def _fetch_der_certificate(uri: str) -> bytes:
    try:
        with urllib.request.urlopen(uri, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise ChainCompletionError(
            f"{uri}: could not fetch the issuer certificate — {exc}"
        ) from exc


@lru_cache(maxsize=8)
def completing_ssl_context(host: str, port: int = 443) -> ssl.SSLContext:
    """A fully verifying context that also trusts ``host``'s missing issuers.

    Cached per host for the life of the process so a multi-document fetch from
    one host does not re-probe. The returned context keeps
    ``check_hostname`` and ``CERT_REQUIRED`` from
    :func:`ssl.create_default_context`.
    """
    leaf = fetch_leaf_der(host, port)
    uris = ca_issuers_uris(leaf)
    if not uris:
        raise ChainCompletionError(
            f"{host}: certificate chain is incomplete and the leaf certificate has no "
            "AIA caIssuers URI to complete it from"
        )

    context = ssl.create_default_context()
    added = 0
    errors: list[str] = []
    for uri in uris:
        try:
            blob = _fetch_der_certificate(uri)
            context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(blob))
        except (ChainCompletionError, ssl.SSLError, ValueError) as exc:
            errors.append(f"{uri}: {exc}")
            continue
        added += 1
    if added == 0:
        raise ChainCompletionError(
            f"{host}: none of the AIA caIssuers certificates could be used — " + "; ".join(errors)
        )
    return context


def host_of(url: str) -> tuple[str, int]:
    """Split ``url`` into ``(hostname, port)``, defaulting the port by scheme."""
    parts = urlsplit(url)
    if not parts.hostname:
        raise ChainCompletionError(f"{url}: no hostname to probe")
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)
