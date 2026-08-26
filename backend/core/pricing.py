"""Per-token prices, read from OpenRouter (story S4.2.2's cost estimate).

Finding D1 says: OpenRouter reports what a call actually cost, so do not build
a price table. That settles *accounting* and leaves one gap — a report's cost
estimate has to be shown **before** the first call is made, and there is no
actual cost yet to report.

This module closes the gap without reintroducing what D1 forbids. The prices
come from OpenRouter's own public model catalogue (``GET /api/v1/models``, no
key required), fetched at most once per process, so there is still nothing to
maintain when a price changes and no figure in this repository that can go
stale. It is the same source of truth as D1's, read one step earlier.

**A missing price is not an error.** The catalogue may be unreachable, or may
not list a model at all. Every failure returns ``None`` and the caller reports
"estimate unavailable" with the reason — which is honest, and better than an
invented number sitting next to a Launch button.

**Prices are per token, as strings, in USD.** OpenRouter sends
``{"prompt": "0.00000015", "completion": "0.0000006"}``; they are parsed to
floats here so no caller has to know that.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import httpx

#: The public catalogue. Unauthenticated on purpose: an estimate must be
#: available on the setup screen before a key has been entered.
MODELS_URL = "https://openrouter.ai/api/v1/models"

#: Short, because this sits in front of a user waiting to launch a report. A
#: slow catalogue costs an estimate, not the run.
DEFAULT_TIMEOUT_SECONDS = 6.0


@dataclass(frozen=True)
class ModelPrice:
    """What one model charges, per token, in USD."""

    model_id: str
    prompt: float
    completion: float

    def cost_of(self, *, prompt_tokens: float, completion_tokens: float) -> float:
        return prompt_tokens * self.prompt + completion_tokens * self.completion


#: ``model_id -> ModelPrice | None``. ``None`` is cached too: a model the
#: catalogue does not list will not start listing it mid-process, and retrying
#: the fetch on every estimate would add six seconds to each one.
_CACHE: dict[str, ModelPrice | None] = {}
_LOCK = threading.Lock()
_FAILURE: str | None = None


def price_of(
    model_id: str, *, client: httpx.Client | None = None, refresh: bool = False
) -> ModelPrice | None:
    """The per-token price of ``model_id``, or ``None`` if it cannot be known.

    ``client`` is the seam the tests use — the catalogue is real HTTP, and a
    test that reached it would be both slow and dependent on today's prices.
    """
    with _LOCK:
        if refresh:
            _CACHE.clear()
        if model_id in _CACHE:
            return _CACHE[model_id]
        _load(client)
        return _CACHE.get(model_id)


def failure_reason() -> str | None:
    """Why the last catalogue fetch failed, if it did. For the estimate's basis."""
    return _FAILURE


def reset() -> None:
    """Drop the cached catalogue. For tests."""
    global _FAILURE
    with _LOCK:
        _CACHE.clear()
        _FAILURE = None


def _load(client: httpx.Client | None) -> None:
    """Fetch the whole catalogue into the cache. Never raises."""
    global _FAILURE
    owned = client is None
    http = client or httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
    try:
        response = http.get(MODELS_URL)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - a missing price is not an error
        _FAILURE = f"{type(exc).__name__}: {exc}"
        return
    finally:
        if owned:
            http.close()

    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        _FAILURE = "the model catalogue did not contain a 'data' list"
        return

    _FAILURE = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        parsed = _parse(entry.get("pricing"))
        if isinstance(model_id, str) and parsed is not None:
            prompt, completion = parsed
            _CACHE[model_id] = ModelPrice(
                model_id=model_id, prompt=prompt, completion=completion
            )


def _parse(pricing: object) -> tuple[float, float] | None:
    """``{"prompt": "0.00000015", ...}`` as two floats, or ``None``.

    A free model reports ``"0"``, which is a real price and must survive; only
    an absent or unparseable field is a miss.
    """
    if not isinstance(pricing, dict):
        return None
    try:
        return float(pricing["prompt"]), float(pricing["completion"])
    except (KeyError, TypeError, ValueError):
        return None
