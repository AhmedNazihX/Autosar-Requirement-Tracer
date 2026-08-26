"""OpenRouter embeddings with a SQLite content-hash cache (story S1.5.1).

Every embedding in this project comes from OpenRouter (design spec §3): one
provider, OpenAI-compatible, no local model and no torch. The model ID comes
from the manifest's ``models.embedding`` and is never hard-coded here — a
pinned-model change has to be a manifest edit, because it changes the vector
space.

Three things this module exists to get right:

* **The cache must actually hit.** Vectors are cached in SQLite keyed on the
  SHA-256 of the *exact text sent to the model* together with the model ID
  (``embedding_cache``'s primary key is ``(content_hash, model_id)``, so the
  model participates in the key as its own column rather than being folded
  into the digest). Re-ingesting an unchanged corpus must send **zero**
  inputs — that is story S1.5.1's acceptance criterion, and it is also what
  makes a re-run cheap enough to be routine.
* **Reassembly must not scramble vectors.** Only the uncached subset is sent,
  so the response is *shorter* than the caller's list and in a different
  order. Mispairing a vector with a chunk produces an index that looks
  healthy and retrieves nonsense, which no test of counts would catch — so
  the response is keyed back through the content hash it was requested for,
  its ``index`` field is validated against what was sent, and every output
  slot is asserted filled before returning.
* **A missing key must be actionable, at call time.** ``core.config`` keeps
  ``OPENROUTER_API_KEY`` optional on purpose so the server boots without it
  and the first-run setup screen (story S3.6.1) can guide the user. So this
  module raises :class:`EmbeddingError` naming the variable and the file when
  a call is actually attempted, and never at import.

The network call sits behind the injected :data:`Transport` parameter, so
tests substitute a fake and *prove* what was sent — no test ever reaches
OpenRouter.

Cost is read from OpenRouter's own ``usage.cost`` field rather than estimated
from a price table: it is the real charge, and there is no table to maintain
when prices move.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

import openai

from core import db
from core.config import Settings, get_settings
from core.manifest import ProjectManifest

#: OpenAI-compatible endpoint. Locked by CLAUDE.md; not configurable per call.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: Inputs per request. The API takes a list; 64 is conservative enough to keep
#: a single failure cheap to retry and large enough that the whole corpus is
#: ~60 requests rather than ~3600.
DEFAULT_BATCH_SIZE = 64

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 0.5
DEFAULT_BACKOFF_CAP_SECONDS = 20.0
DEFAULT_TIMEOUT_SECONDS = 120.0

#: The environment variable and the file it belongs in, named in every error
#: about a missing key so the message is actionable on its own.
API_KEY_ENV_VAR = "OPENROUTER_API_KEY"
API_KEY_ENV_FILE = "backend/.env"
API_KEY_ENV_EXAMPLE = "backend/.env.example"

#: HTTP statuses worth retrying: rate limits, request timeouts and the 5xx
#: family. Anything else (401, 400, 404) is a fault retrying cannot fix.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class EmbeddingError(Exception):
    """An embedding call failed, or cannot be attempted.

    ``retryable`` distinguishes "the service was momentarily unavailable"
    from "this request is wrong" — only the former is worth a backoff.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def content_hash(text: str) -> str:
    """SHA-256 of the exact text that is sent to the model, hex-encoded."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def api_key(settings: Settings | None = None) -> str:
    """The configured OpenRouter key, or a clear, actionable error.

    Called at request time only — never at import — so a fresh clone with no
    key can still boot the API and run ingestion with ``--skip-embeddings``.
    """
    resolved = settings if settings is not None else get_settings()
    key = (resolved.openrouter_api_key or "").strip()
    if not key:
        raise EmbeddingError(
            f"{API_KEY_ENV_VAR} is not set, so no embedding can be computed. "
            f"Put a key in {API_KEY_ENV_FILE} (copy {API_KEY_ENV_EXAMPLE} and fill in "
            f"{API_KEY_ENV_VAR}=sk-or-...), or re-run with --skip-embeddings to build "
            "the SQLite registry and the BM25 index without vectors."
        )
    return key


# --------------------------------------------------------------------------
# transport
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingBatch:
    """One provider response: vectors in request order, plus what it cost."""

    vectors: list[list[float]]
    prompt_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0


#: ``(texts, model_id) -> EmbeddingBatch``. Must return one vector per input,
#: in input order, and raise :class:`EmbeddingError` on failure — with
#: ``retryable=True`` when a retry could plausibly succeed.
Transport = Callable[[Sequence[str], str], EmbeddingBatch]

#: ``(model_id, done, total)`` — the same three-argument shape as the fetchers'
#: hooks, so story S3.6.2 can stream any of them over SSE unchanged.
ProgressCallback = Callable[[str, int, int | None], None]


def _classify(exc: Exception) -> EmbeddingError:
    """Wrap an SDK exception as :class:`EmbeddingError`, marking retryability."""
    if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
        return EmbeddingError(f"OpenRouter connection failed — {exc}", retryable=True)
    if isinstance(exc, openai.APIStatusError):
        status = exc.status_code
        detail = f"OpenRouter returned HTTP {status} — {exc}"
        if status == 401:
            detail = (
                f"OpenRouter rejected the credentials (HTTP 401). Check {API_KEY_ENV_VAR} "
                f"in {API_KEY_ENV_FILE}."
            )
        return EmbeddingError(detail, retryable=status in RETRYABLE_STATUS)
    return EmbeddingError(f"embedding request failed — {exc}")


def _batch_from_response(response: object, expected: int, model_id: str) -> EmbeddingBatch:
    """Convert an OpenAI-compatible embeddings response, order-checked.

    The response's ``index`` fields are the provider's statement of which
    input each vector belongs to, so they are sorted on and then verified to
    be exactly ``0..expected-1``. A provider that dropped or duplicated an
    input fails here rather than silently shifting every subsequent vector
    onto the wrong chunk.
    """
    data = sorted(getattr(response, "data", []), key=lambda item: item.index)
    if len(data) != expected:
        raise EmbeddingError(
            f"{model_id}: asked for {expected} embedding(s) and got {len(data)} back — "
            "refusing to pair vectors with chunks by position"
        )
    if [item.index for item in data] != list(range(expected)):
        raise EmbeddingError(
            f"{model_id}: response indices {[item.index for item in data]} are not "
            f"0..{expected - 1} — refusing to pair vectors with chunks by position"
        )

    usage = getattr(response, "usage", None)
    extra = (getattr(usage, "model_extra", None) or {}) if usage is not None else {}
    return EmbeddingBatch(
        vectors=[list(item.embedding) for item in data],
        prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        # OpenRouter reports the actual charge in the usage payload. Using it
        # beats estimating from a hard-coded price table: more accurate, and
        # nothing to maintain when prices move.
        cost_usd=float(extra.get("cost") or 0.0),
    )


def openrouter_transport(
    *,
    base_url: str = OPENROUTER_BASE_URL,
    settings: Settings | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Transport:
    """A :data:`Transport` backed by OpenRouter's ``/embeddings`` endpoint.

    The SDK client is constructed on the first call, so importing this module
    — or building an :class:`EmbeddingClient` — never needs a key.
    """
    holder: dict[str, openai.OpenAI] = {}

    def transport(texts: Sequence[str], model_id: str) -> EmbeddingBatch:
        client = holder.get("client")
        if client is None:
            client = openai.OpenAI(base_url=base_url, api_key=api_key(settings), timeout=timeout)
            holder["client"] = client
        inputs = list(texts)
        try:
            response = client.embeddings.create(model=model_id, input=inputs)
        except Exception as exc:  # noqa: BLE001 - re-raised as EmbeddingError
            raise _classify(exc) from exc
        return _batch_from_response(response, len(inputs), model_id)

    return transport


# --------------------------------------------------------------------------
# usage accounting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingUsage:
    """What a set of embedding calls consumed, tagged with its purpose.

    ``purpose`` is the string story S3.1.1's cost meter aggregates on; this
    module only ever produces ``"embed"``.
    """

    purpose: str = "embed"
    calls: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    def plus(self, batch: EmbeddingBatch, *, retries: int = 0) -> EmbeddingUsage:
        return replace(
            self,
            calls=self.calls + 1,
            retries=self.retries + retries,
            prompt_tokens=self.prompt_tokens + batch.prompt_tokens,
            total_tokens=self.total_tokens + batch.total_tokens,
            cost_usd=self.cost_usd + batch.cost_usd,
        )


@dataclass(frozen=True)
class EmbeddingResult:
    """Vectors for the caller's texts, in the caller's order, plus accounting.

    ``computed + cached + duplicates == len(texts)``. ``duplicates`` are
    positions whose text repeated inside the same request and were served
    from the single input that was sent for it.
    """

    vectors: list[list[float]]
    computed: int = 0
    cached: int = 0
    duplicates: int = 0
    usage: EmbeddingUsage = EmbeddingUsage()


# --------------------------------------------------------------------------
# the client
# --------------------------------------------------------------------------


@dataclass
class EmbeddingClient:
    """Batching, caching and retrying wrapper around a :data:`Transport`.

    Holds no connection: the SQLite connection is passed per call so the
    ingestion CLI and (later) query-time retrieval can use their own.
    """

    model_id: str
    transport: Transport
    batch_size: int = DEFAULT_BATCH_SIZE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    backoff_cap_seconds: float = DEFAULT_BACKOFF_CAP_SECONDS
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {self.batch_size}")
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {self.max_attempts}")

    def embed(
        self,
        texts: Sequence[str],
        conn: sqlite3.Connection | None = None,
        *,
        purpose: str = "embed",
        on_progress: ProgressCallback | None = None,
    ) -> EmbeddingResult:
        """Embed ``texts``, serving what ``conn``'s cache already holds.

        Returns one vector per input **in the caller's order**. When ``conn``
        is ``None`` nothing is cached and every input is sent, which is only
        useful for a one-off query embedding.
        """
        ordered = list(texts)
        vectors: list[list[float] | None] = [None] * len(ordered)

        # content hash -> the caller positions wanting that exact text, in
        # caller order. Insertion order is therefore caller order, which is
        # what keeps the request deterministic across runs.
        pending: dict[str, list[int]] = {}
        cached = 0
        for position, text in enumerate(ordered):
            digest = content_hash(text)
            if conn is not None:
                hit = db.get_embedding(conn, digest, self.model_id)
                if hit is not None:
                    vectors[position] = hit
                    cached += 1
                    continue
            pending.setdefault(digest, []).append(position)

        digests = list(pending)
        usage = EmbeddingUsage(purpose=purpose)
        done = 0
        for start in range(0, len(digests), self.batch_size):
            window = digests[start : start + self.batch_size]
            batch_texts = [ordered[pending[digest][0]] for digest in window]
            batch, retries = self._call(batch_texts)
            usage = usage.plus(batch, retries=retries)
            if len(batch.vectors) != len(window):
                # The stock transport already checks this against the response's
                # own index fields; re-checking here means a custom transport
                # cannot shift every later vector onto the wrong chunk either.
                raise EmbeddingError(
                    f"{self.model_id}: transport returned {len(batch.vectors)} vector(s) for "
                    f"{len(window)} input(s) — refusing to pair vectors with chunks by position"
                )
            for digest, vector in zip(window, batch.vectors, strict=True):
                if conn is not None:
                    db.put_embedding(conn, digest, self.model_id, vector)
                for position in pending[digest]:
                    vectors[position] = vector
            done += len(window)
            if on_progress is not None:
                on_progress(self.model_id, done, len(digests))

        unfilled = [index for index, vector in enumerate(vectors) if vector is None]
        if unfilled:
            raise EmbeddingError(
                f"{self.model_id}: {len(unfilled)} of {len(ordered)} input(s) came back "
                f"without a vector (first at position {unfilled[0]}) — refusing to return "
                "a partially filled result"
            )

        duplicates = len(ordered) - cached - len(digests)
        return EmbeddingResult(
            vectors=[vector for vector in vectors if vector is not None],
            computed=len(digests),
            cached=cached,
            duplicates=duplicates,
            usage=usage,
        )

    def _call(self, texts: Sequence[str]) -> tuple[EmbeddingBatch, int]:
        """One transport call with bounded exponential backoff.

        Returns the batch and how many retries it took. A 90-minute ingestion
        must not die on a single 429; an unretryable fault must not be
        retried five times before reporting a wrong API key.
        """
        attempt = 1
        while True:
            try:
                return self.transport(texts, self.model_id), attempt - 1
            except EmbeddingError as exc:
                if not exc.retryable:
                    raise
                if attempt >= self.max_attempts:
                    raise EmbeddingError(
                        f"{exc} — gave up after {attempt} attempt(s) on a batch of "
                        f"{len(texts)} input(s)",
                        retryable=True,
                    ) from exc
                delay = min(
                    self.backoff_seconds * (2 ** (attempt - 1)), self.backoff_cap_seconds
                )
                self.sleep(delay)
                attempt += 1


def client_for(
    manifest: ProjectManifest,
    *,
    transport: Transport | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    settings: Settings | None = None,
) -> EmbeddingClient:
    """Build a client for ``manifest``'s pinned embedding model.

    The model ID comes from ``models.embedding`` — the only place it is
    allowed to come from (CLAUDE.md).
    """
    return EmbeddingClient(
        model_id=manifest.models.embedding,
        transport=transport if transport is not None else openrouter_transport(settings=settings),
        batch_size=batch_size,
    )
