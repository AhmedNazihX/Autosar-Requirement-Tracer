"""Tests for the OpenRouter embeddings client and its SQLite cache.

No test here reaches OpenRouter: every one injects a fake
:data:`~core.embeddings.Transport` and asserts on *what was sent*. That is the
only way to prove the two properties that matter and that counting rows cannot
see — that a warm cache sends nothing, and that a partially warm cache sends
exactly the missing subset and pairs the returned vectors back onto the right
chunks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core import db
from core.config import Settings
from core.embeddings import (
    API_KEY_ENV_FILE,
    API_KEY_ENV_VAR,
    EmbeddingBatch,
    EmbeddingClient,
    EmbeddingError,
    api_key,
    client_for,
    content_hash,
    openrouter_transport,
)
from core.manifest import load_manifest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")

MODEL = "test/embedding-v1"
OTHER_MODEL = "test/embedding-v2"


def fingerprint(text: str) -> list[float]:
    """A deterministic, text-dependent vector, so mispairing is detectable.

    A constant vector would let every wrong pairing pass; this one is a
    function of the text, so asserting ``vector == fingerprint(text)`` asserts
    the pairing itself.
    """
    return [float(len(text)), float(sum(text.encode("utf-8")) % 1000), 0.5]


class FakeTransport:
    """Records every batch it is asked for, and can fail on demand."""

    def __init__(self, *, failures: list[EmbeddingError] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.failures = list(failures or [])

    @property
    def inputs(self) -> list[str]:
        return [text for call in self.calls for text in call]

    def __call__(self, texts, model_id: str) -> EmbeddingBatch:
        self.calls.append(list(texts))
        if self.failures:
            raise self.failures.pop(0)
        return EmbeddingBatch(
            vectors=[fingerprint(text) for text in texts],
            prompt_tokens=len(texts),
            total_tokens=len(texts),
            cost_usd=1e-7 * len(texts),
        )


@pytest.fixture
def conn(tmp_path: Path):
    connection = db.connect(tmp_path / "cache.db")
    db.migrate(connection)
    yield connection
    connection.close()


def keyless_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A ``Settings`` with no key, ignoring the developer's real ``.env``."""
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    return Settings(_env_file=None)


# --------------------------------------------------------------------------
# the cache
# --------------------------------------------------------------------------


def test_the_second_pass_over_the_same_chunks_sends_zero_inputs(conn):
    """Story S1.5.1's acceptance criterion, stated as a test."""
    transport = FakeTransport()
    client = EmbeddingClient(model_id=MODEL, transport=transport)
    texts = ["SWS_Can_00011 body", "SWS_Can_00012 body", "context prose"]

    first = client.embed(texts, conn)
    assert first.computed == 3
    assert first.cached == 0

    second = client.embed(texts, conn)
    assert second.computed == 0, "a warm cache must send nothing"
    assert second.cached == 3
    assert len(transport.calls) == 1, "the second pass must make no request at all"
    assert second.vectors == first.vectors


def test_only_the_uncached_subset_is_sent_and_reassembles_in_caller_order(conn):
    """The easy thing to get wrong: vectors silently paired to the wrong chunk."""
    transport = FakeTransport()
    client = EmbeddingClient(model_id=MODEL, transport=transport)
    texts = [f"chunk {index}" for index in range(5)]

    # Warm exactly positions 1 and 3.
    client.embed([texts[1], texts[3]], conn)
    transport.calls.clear()

    result = client.embed(texts, conn)

    assert len(transport.calls) == 1, "the missing subset must go out in one batch"
    assert transport.calls[0] == [texts[0], texts[2], texts[4]], (
        "exactly the uncached texts, in the caller's order"
    )
    assert result.computed == 3
    assert result.cached == 2
    # The pairing itself, position by position — not just the count.
    for text, vector in zip(texts, result.vectors, strict=True):
        assert vector == fingerprint(text)


def test_a_repeated_text_inside_one_request_is_sent_once(conn):
    transport = FakeTransport()
    client = EmbeddingClient(model_id=MODEL, transport=transport)
    texts = ["same", "other", "same", "same"]

    result = client.embed(texts, conn)

    assert transport.inputs == ["same", "other"]
    assert result.computed == 2
    assert result.duplicates == 2
    assert result.cached == 0
    assert result.vectors[0] == result.vectors[2] == result.vectors[3] == fingerprint("same")


def test_a_different_model_id_invalidates_the_cache(conn):
    """A pinned-model change must re-embed, never serve another vector space."""
    texts = ["SWS_Can_00011 body"]
    first = FakeTransport()
    EmbeddingClient(model_id=MODEL, transport=first).embed(texts, conn)
    assert len(first.calls) == 1

    second = FakeTransport()
    result = EmbeddingClient(model_id=OTHER_MODEL, transport=second).embed(texts, conn)

    assert len(second.calls) == 1, "a different model must not read the first model's cache"
    assert result.computed == 1
    assert db.get_embedding(conn, content_hash(texts[0]), MODEL) is not None
    assert db.get_embedding(conn, content_hash(texts[0]), OTHER_MODEL) is not None


def test_a_changed_text_re_embeds_only_that_chunk(conn):
    transport = FakeTransport()
    client = EmbeddingClient(model_id=MODEL, transport=transport)
    client.embed(["a", "b"], conn)
    transport.calls.clear()

    result = client.embed(["a", "b modified"], conn)

    assert transport.inputs == ["b modified"]
    assert (result.computed, result.cached) == (1, 1)


def test_no_connection_means_no_cache(conn):
    transport = FakeTransport()
    client = EmbeddingClient(model_id=MODEL, transport=transport)
    client.embed(["x"])
    client.embed(["x"])
    assert len(transport.calls) == 2


def test_content_hash_is_the_sha256_of_the_exact_text():
    import hashlib

    assert content_hash("Can_Write") == hashlib.sha256(b"Can_Write").hexdigest()
    assert content_hash("Can_Write ") != content_hash("Can_Write")


# --------------------------------------------------------------------------
# batching
# --------------------------------------------------------------------------


def test_batching_splits_at_the_configured_size(conn):
    transport = FakeTransport()
    client = EmbeddingClient(model_id=MODEL, transport=transport, batch_size=3)
    texts = [f"chunk {index}" for index in range(7)]

    result = client.embed(texts, conn)

    assert [len(call) for call in transport.calls] == [3, 3, 1]
    assert transport.inputs == texts
    for text, vector in zip(texts, result.vectors, strict=True):
        assert vector == fingerprint(text)
    assert result.usage.calls == 3


def test_a_zero_batch_size_is_rejected():
    with pytest.raises(ValueError, match="batch_size"):
        EmbeddingClient(model_id=MODEL, transport=FakeTransport(), batch_size=0)


def test_usage_accumulates_across_batches(conn):
    transport = FakeTransport()
    client = EmbeddingClient(model_id=MODEL, transport=transport, batch_size=2)
    result = client.embed(["a", "b", "c"], conn)
    assert result.usage.calls == 2
    assert result.usage.total_tokens == 3
    assert result.usage.cost_usd == pytest.approx(3e-7)
    assert result.usage.purpose == "embed"


# --------------------------------------------------------------------------
# the missing key
# --------------------------------------------------------------------------


def test_a_missing_key_raises_a_message_naming_the_variable_and_the_file(monkeypatch):
    with pytest.raises(EmbeddingError) as error:
        api_key(keyless_settings(monkeypatch))
    message = str(error.value)
    assert API_KEY_ENV_VAR in message
    assert API_KEY_ENV_FILE in message
    assert "--skip-embeddings" in message, "the message must say what to do instead"


def test_a_missing_key_raises_at_call_time_not_at_construction(monkeypatch):
    settings = keyless_settings(monkeypatch)
    transport = openrouter_transport(settings=settings)
    client = EmbeddingClient(model_id=MODEL, transport=transport)
    with pytest.raises(EmbeddingError, match=API_KEY_ENV_VAR):
        client.embed(["anything"])


def test_importing_the_module_without_a_key_does_not_raise():
    """Run in a subprocess, because this process already has a key loaded.

    ``core.config`` keeps the key optional so the server boots without it and
    the first-run setup screen can guide the user; an import-time raise here
    would defeat that, and only a fresh interpreter can prove it does not.
    """
    environment = {key: value for key, value in os.environ.items() if key != API_KEY_ENV_VAR}
    environment["PYTHONPATH"] = str(BACKEND_DIR)
    completed = subprocess.run(
        [sys.executable, "-c", "import core.embeddings; print('imported')"],
        cwd=str(BACKEND_DIR),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "imported" in completed.stdout


# --------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------


def test_a_429_is_retried_and_then_succeeds(conn):
    transport = FakeTransport(
        failures=[
            EmbeddingError("HTTP 429", retryable=True),
            EmbeddingError("HTTP 429", retryable=True),
        ]
    )
    slept: list[float] = []
    client = EmbeddingClient(
        model_id=MODEL,
        transport=transport,
        backoff_seconds=0.01,
        sleep=slept.append,
    )

    result = client.embed(["chunk"], conn)

    assert result.computed == 1
    assert result.vectors == [fingerprint("chunk")]
    assert len(transport.calls) == 3, "two failures then one success"
    assert slept == [0.01, 0.02], "bounded exponential backoff"
    assert result.usage.retries == 2


def test_retries_are_bounded_and_the_final_error_says_so(conn):
    transport = FakeTransport(failures=[EmbeddingError("HTTP 503", retryable=True)] * 10)
    client = EmbeddingClient(
        model_id=MODEL,
        transport=transport,
        max_attempts=3,
        backoff_seconds=0.0,
        sleep=lambda _: None,
    )

    with pytest.raises(EmbeddingError, match="gave up after 3 attempt"):
        client.embed(["chunk"], conn)
    assert len(transport.calls) == 3


def test_backoff_is_capped(conn):
    transport = FakeTransport(failures=[EmbeddingError("429", retryable=True)] * 4)
    slept: list[float] = []
    client = EmbeddingClient(
        model_id=MODEL,
        transport=transport,
        max_attempts=6,
        backoff_seconds=1.0,
        backoff_cap_seconds=2.0,
        sleep=slept.append,
    )
    client.embed(["chunk"], conn)
    assert slept == [1.0, 2.0, 2.0, 2.0]


def test_an_unretryable_error_is_not_retried(conn):
    transport = FakeTransport(failures=[EmbeddingError("HTTP 401 bad key")])
    client = EmbeddingClient(model_id=MODEL, transport=transport, sleep=lambda _: None)
    with pytest.raises(EmbeddingError, match="401"):
        client.embed(["chunk"], conn)
    assert len(transport.calls) == 1


def test_nothing_is_cached_when_the_call_fails(conn):
    transport = FakeTransport(failures=[EmbeddingError("HTTP 400 bad request")])
    client = EmbeddingClient(model_id=MODEL, transport=transport)
    with pytest.raises(EmbeddingError):
        client.embed(["chunk"], conn)
    assert db.get_embedding(conn, content_hash("chunk"), MODEL) is None


# --------------------------------------------------------------------------
# malformed responses
# --------------------------------------------------------------------------


def test_a_short_response_is_refused_rather_than_shifted(conn):
    def broken(texts, model_id):
        return EmbeddingBatch(vectors=[fingerprint(texts[0])])

    client = EmbeddingClient(model_id=MODEL, transport=broken)
    with pytest.raises(EmbeddingError, match="refusing to pair vectors with chunks"):
        client.embed(["a", "b"], conn)
    assert db.get_embedding(conn, content_hash("a"), MODEL) is None


# --------------------------------------------------------------------------
# the manifest is the only source of the model id
# --------------------------------------------------------------------------


def test_client_for_takes_the_model_id_from_the_manifest():
    client = client_for(MANIFEST, transport=FakeTransport())
    assert client.model_id == MANIFEST.models.embedding
    assert client.model_id == "openai/text-embedding-3-small"
