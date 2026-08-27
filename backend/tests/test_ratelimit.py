"""The token bucket in front of ``POST /chat`` (story S6.3.1).

Two halves, tested separately because they fail differently.

:mod:`api.ratelimit` is pure arithmetic with the clock injected, so the bucket
tests pass timestamps rather than sleeping — a rate limiter tested with
``time.sleep`` is a slow test that still cannot assert what happens after an
hour.

The HTTP half asserts the contract the frontend was changed to match: 429 with
a ``Retry-After`` header, and refusal *before* the readiness check, so a client
looping against an unindexed backend is answered cheaply.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import chat as chat_module
from api import deps
from api.ratelimit import MAX_TRACKED_CLIENTS, RateLimiter, TokenBucket

MINUTE = 60.0


# --------------------------------------------------------------------------
# the bucket
# --------------------------------------------------------------------------


def test_a_new_client_may_spend_its_whole_burst_at_once():
    """A person asks three questions in ten seconds; that is not a flood."""
    limiter = RateLimiter(per_minute=12, burst=6)

    waits = [limiter.check("a", now=0.0) for _ in range(6)]

    assert waits == [0.0] * 6


def test_the_request_after_the_burst_is_refused():
    limiter = RateLimiter(per_minute=12, burst=3)
    for _ in range(3):
        limiter.check("a", now=0.0)

    assert limiter.check("a", now=0.0) > 0


def test_the_wait_it_returns_is_how_long_the_refill_actually_takes():
    """``Retry-After`` has to be true, or a well-behaved client still fails."""
    limiter = RateLimiter(per_minute=60, burst=1)  # one token per second
    limiter.check("a", now=0.0)

    wait = limiter.check("a", now=0.0)

    assert wait == pytest.approx(1.0)
    assert limiter.check("a", now=wait) == 0.0


def test_waiting_refills_the_bucket():
    limiter = RateLimiter(per_minute=60, burst=5)
    for _ in range(5):
        limiter.check("a", now=0.0)

    assert limiter.check("a", now=0.5) > 0, "half a second buys nothing"
    assert limiter.check("a", now=3.0) == 0.0


def test_the_bucket_never_refills_past_its_burst():
    """An idle hour must not buy an hour's worth of requests in one go."""
    limiter = RateLimiter(per_minute=60, burst=4)

    for _ in range(4):
        assert limiter.check("a", now=3600.0) == 0.0
    assert limiter.check("a", now=3600.0) > 0


def test_a_sustained_client_is_held_to_the_rate_not_the_burst():
    """The property that actually bounds the bill over a long loop.

    Six per minute with a burst of five: a client hammering once a second for
    a minute gets its five free, then one every ten seconds — ten in the
    minute, not sixty. This is the number that decides what a runaway costs.
    """
    limiter = RateLimiter(per_minute=6, burst=5)

    allowed = sum(
        1 for tick in range(60) if limiter.check("a", now=float(tick)) == 0.0
    )

    assert allowed == 10


def test_clients_have_separate_buckets():
    limiter = RateLimiter(per_minute=12, burst=1)
    limiter.check("a", now=0.0)

    assert limiter.check("b", now=0.0) == 0.0
    assert limiter.check("a", now=0.0) > 0


def test_a_bucket_that_never_refills_says_so_rather_than_dividing_by_zero():
    bucket = TokenBucket(capacity=1, refill_per_second=0.0, tokens=0.0, updated_at=0.0)

    assert bucket.take(now=10.0) == float("inf")


def test_the_number_of_tracked_clients_is_bounded():
    """The bucket map is keyed on something a caller controls, so it is capped."""
    limiter = RateLimiter(per_minute=12, burst=1)
    for index in range(MAX_TRACKED_CLIENTS + 50):
        limiter.check(f"client-{index}", now=float(index))

    assert len(limiter.buckets) <= MAX_TRACKED_CLIENTS


def test_eviction_drops_the_stalest_client_not_the_newest():
    limiter = RateLimiter(per_minute=12, burst=1)
    limiter.check("oldest", now=0.0)
    for index in range(MAX_TRACKED_CLIENTS):
        limiter.check(f"client-{index}", now=float(index + 1))

    assert "oldest" not in limiter.buckets


def test_reset_forgets_everyone():
    limiter = RateLimiter(per_minute=12, burst=1)
    limiter.check("a", now=0.0)

    limiter.reset()

    assert limiter.check("a", now=0.0) == 0.0


# --------------------------------------------------------------------------
# POST /chat
# --------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    """A chat endpoint with a small bucket and no corpus behind it.

    Unindexed on purpose: the limit is checked before readiness, so this
    exercises the flood without building an index for a test that never gets
    as far as retrieval.
    """
    monkeypatch.setattr(
        chat_module, "LIMITER", RateLimiter(per_minute=60, burst=3), raising=True
    )
    app = FastAPI()
    app.include_router(chat_module.router)
    app.state.reqtrace = deps.AppState(error="no corpus for this test")
    return TestClient(app)


def ask(client, message: str = "what does SWS_Can_00011 require?"):
    return client.post("/chat", json={"thread_id": "t1", "message": message})


def test_a_flood_is_refused_once_the_burst_is_spent(client):
    """S6.3.1's acceptance: the flood test."""
    allowed = [ask(client).status_code for _ in range(3)]
    flooded = ask(client)

    assert allowed == [200, 200, 200]
    assert flooded.status_code == 429


def test_a_refusal_says_how_long_to_wait(client):
    for _ in range(4):
        response = ask(client)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_the_refusal_explains_itself(client):
    for _ in range(4):
        response = ask(client)

    assert "Too many" in response.json()["detail"]


def test_the_limit_is_checked_before_the_corpus_is(client):
    """A loop against an unindexed backend is the same runaway.

    Without this ordering the cheap failure path would be the *unlimited* one,
    which is exactly backwards.
    """
    for _ in range(3):
        assert ask(client).status_code == 200

    assert ask(client).status_code == 429


def test_the_limit_is_per_client_not_per_thread(client):
    """Opening a new thread must not hand the same loop a fresh budget."""
    for _ in range(3):
        assert ask(client).status_code == 200

    fresh = client.post("/chat", json={"thread_id": "t2", "message": "hello"})

    assert fresh.status_code == 429


def test_a_malformed_body_is_still_a_422_not_a_429(client):
    """Validation is the frontend's bug; the limit is the user's behaviour."""
    response = client.post("/chat", json={"thread_id": "t1"})

    assert response.status_code == 422
