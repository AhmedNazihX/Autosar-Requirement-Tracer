"""Tests for ingestion/tls_chain.py.

``www.autosar.org`` presents only its leaf certificate and omits the
intermediate, which OpenSSL (and therefore Python) rejects even though
browsers accept it. The fetcher recovers by fetching the missing issuer from
the leaf's AIA extension. The parts that can be tested offline — the DER
scan and the error classifier — are tested here; the parts that need the
network are opt-in and skip by default.
"""

from __future__ import annotations

import os
import ssl
import urllib.error

import pytest

from ingestion import tls_chain

RUN_NETWORK_TESTS = os.environ.get("REQTRACE_TLS_NETWORK_TESTS") == "1"

# id-ad-caIssuers OID TLV, then GeneralName [6] with a short-form length.
OID = bytes.fromhex("06082b06010505073002")


def aia_blob(uri: str) -> bytes:
    return OID + bytes([0x86, len(uri)]) + uri.encode("ascii")


# --------------------------------------------------------------------------
# the DER scan
# --------------------------------------------------------------------------


def test_finds_a_single_ca_issuers_uri():
    der = b"\x30\x82\x01\x00padding" + aia_blob("http://ca.example/int.crt") + b"trailing"
    assert tls_chain.ca_issuers_uris(der) == ["http://ca.example/int.crt"]


def test_finds_several_ca_issuers_uris_in_order():
    der = aia_blob("http://a.example/1.crt") + b"\x00\x01" + aia_blob("https://b.example/2.cer")
    assert tls_chain.ca_issuers_uris(der) == [
        "http://a.example/1.crt",
        "https://b.example/2.cer",
    ]


def test_no_extension_yields_no_uris():
    assert tls_chain.ca_issuers_uris(b"\x30\x82\x01\x00 no aia here at all") == []


def test_ignores_a_non_uri_general_name():
    # [2] dNSName rather than [6] uniformResourceIdentifier.
    der = OID + bytes([0x82, 11]) + b"ca.example."
    assert tls_chain.ca_issuers_uris(der) == []


def test_ignores_a_non_http_scheme():
    der = OID + bytes([0x86, len("ldap://ca.example/cn=x")]) + b"ldap://ca.example/cn=x"
    assert tls_chain.ca_issuers_uris(der) == []


def test_ignores_a_long_form_length():
    """Only the short form is handled; a long form must be skipped, not misread."""
    der = OID + bytes([0x86, 0x81, 4]) + b"http"
    assert tls_chain.ca_issuers_uris(der) == []


def test_truncated_extension_does_not_raise():
    assert tls_chain.ca_issuers_uris(OID) == []
    assert tls_chain.ca_issuers_uris(OID + b"\x86") == []


# --------------------------------------------------------------------------
# error classification — the retry must be narrow
# --------------------------------------------------------------------------


def make_verify_error(code: int) -> ssl.SSLCertVerificationError:
    error = ssl.SSLCertVerificationError("certificate verify failed")
    error.verify_code = code
    return error


@pytest.mark.parametrize("code", sorted(tls_chain.INCOMPLETE_CHAIN_VERIFY_CODES))
def test_incomplete_chain_codes_are_recognized(code: int):
    error = make_verify_error(code)
    assert tls_chain.is_incomplete_chain_error(error)
    assert tls_chain.is_incomplete_chain_error(urllib.error.URLError(error))


def test_an_expired_certificate_is_not_an_incomplete_chain():
    # X509_V_ERR_CERT_HAS_EXPIRED — a real failure that must NOT be retried.
    error = make_verify_error(10)
    assert not tls_chain.is_incomplete_chain_error(error)
    assert not tls_chain.is_incomplete_chain_error(urllib.error.URLError(error))


def test_a_plain_network_error_is_not_an_incomplete_chain():
    assert not tls_chain.is_incomplete_chain_error(urllib.error.URLError("connection refused"))
    assert not tls_chain.is_incomplete_chain_error(OSError("no route to host"))


# --------------------------------------------------------------------------
# url splitting
# --------------------------------------------------------------------------


def test_host_of_defaults_the_port_by_scheme():
    assert tls_chain.host_of("https://www.example.org/a/b.pdf") == ("www.example.org", 443)
    assert tls_chain.host_of("http://www.example.org/a/b.pdf") == ("www.example.org", 80)
    assert tls_chain.host_of("https://www.example.org:8443/x") == ("www.example.org", 8443)


def test_host_of_rejects_a_url_without_a_host():
    with pytest.raises(tls_chain.ChainCompletionError, match="no hostname"):
        tls_chain.host_of("not-a-url")


# --------------------------------------------------------------------------
# opt-in: the real host
# --------------------------------------------------------------------------


@pytest.mark.skipif(not RUN_NETWORK_TESTS, reason="set REQTRACE_TLS_NETWORK_TESTS=1 to run")
def test_the_real_corpus_host_needs_and_gets_chain_completion():
    context = tls_chain.completing_ssl_context("www.autosar.org")
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert tls_chain.ca_issuers_uris(tls_chain.fetch_leaf_der("www.autosar.org"))
