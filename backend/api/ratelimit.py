"""A token-bucket rate limiter for the endpoints that spend money (story S6.3.1).

**What this is actually for.** ReqTrace is a local, single-user application
(spec §11) reached through the Next.js proxy, so every request arrives from
127.0.0.1 and there is no distributed attacker to defend against. The threat
that is real is a *runaway client*: a component that re-fires on every render,
a retry loop with no backoff, a held-down key. Each ``POST /chat`` starts an
agent turn that calls a model several times, so a loop like that is a bill, not
just load. A bucket bounds the bill without getting in the way of a person
typing, which is what a plain "N per minute" counter cannot do — a person sends
three questions in ten seconds and then nothing for a minute, and a counter
either rejects that burst or is set so high it stops nothing.

**Why a bucket rather than a window.** Burst capacity and sustained rate are
separate numbers here: the burst is what a human does, the refill rate is what
a loop does. A fixed window conflates them, and a sliding window needs a
request log per client for no benefit at this scale.

**The clock is injected.** ``now`` is a parameter, never read inside the
bucket, so a flood test asserts on refill by passing timestamps rather than by
sleeping through them.

Nothing here touches FastAPI: :func:`RateLimiter.check` takes a key and a time
and returns how long to wait. The HTTP shape — 429, ``Retry-After`` — is the
caller's business (``api/chat.py``).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

#: Most distinct clients tracked at once. An unbounded dict keyed on something
#: a caller controls is its own denial of service; on a local app this ceiling
#: is never reached, and if it somehow is, the oldest bucket is dropped —
#: which at worst grants one extra burst to a client that had gone quiet.
MAX_TRACKED_CLIENTS = 1024


@dataclass
class TokenBucket:
    """Capacity that refills at a fixed rate, spent one request at a time."""

    capacity: float
    refill_per_second: float
    tokens: float
    updated_at: float

    def take(self, now: float, cost: float = 1.0) -> float:
        """Spend ``cost`` tokens, or say how long until it is affordable.

        Returns ``0.0`` when the request is allowed, otherwise the seconds to
        wait — which the caller sends as ``Retry-After`` so a well-behaved
        client backs off by the right amount instead of guessing.
        """
        elapsed = max(now - self.updated_at, 0.0)
        self.updated_at = now
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_second)
        if self.tokens >= cost:
            self.tokens -= cost
            return 0.0
        if self.refill_per_second <= 0:
            return float("inf")
        return (cost - self.tokens) / self.refill_per_second


@dataclass
class RateLimiter:
    """One :class:`TokenBucket` per client key.

    ``per_minute`` is the sustained rate and ``burst`` is the depth — how many
    requests may arrive back to back before the rate starts to bind. A client
    is seen for the first time with a full bucket, so the limiter is invisible
    until someone actually loops.
    """

    per_minute: float
    burst: float
    clock: object = field(default=time.monotonic)
    buckets: dict[str, TokenBucket] = field(default_factory=dict)

    def check(self, key: str, *, now: float | None = None) -> float:
        """Seconds the caller must wait, or ``0.0`` if the request may proceed."""
        moment = self.clock() if now is None else now  # type: ignore[operator]
        bucket = self.buckets.get(key)
        if bucket is None:
            if len(self.buckets) >= MAX_TRACKED_CLIENTS:
                self._evict_oldest()
            bucket = TokenBucket(
                capacity=self.burst,
                refill_per_second=self.per_minute / 60.0,
                tokens=self.burst,
                updated_at=moment,
            )
            self.buckets[key] = bucket
        return bucket.take(moment)

    def _evict_oldest(self) -> None:
        oldest = min(self.buckets, key=lambda key: self.buckets[key].updated_at)
        del self.buckets[oldest]

    def reset(self) -> None:
        """Forget every client. For tests, and for a server that reloads."""
        self.buckets.clear()


def client_key(request) -> str:
    """Who is asking, as well as a local app can tell.

    Behind the Next.js proxy this is 127.0.0.1 for everyone, so in practice the
    limit is per-machine rather than per-user. That is the honest scope of the
    defence and it is the right one for a tool that runs on one desktop: it
    stops a loop, and it is not pretending to stop an attacker who could simply
    talk to uvicorn directly.
    """
    client = getattr(request, "client", None)
    return getattr(client, "host", None) or "unknown"
