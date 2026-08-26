"""Tests for core/pricing.py — OpenRouter's catalogue prices (story S4.2.2).

The catalogue is real HTTP, so every test here serves it from an
``httpx.MockTransport``: reaching openrouter.ai would make the suite slow,
network-dependent, and quietly wrong on the day a price changes.
"""

from __future__ import annotations

import httpx
import pytest

from core import pricing

CATALOGUE = {
    "data": [
        {
            "id": "openai/gpt-4o-mini",
            "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
        },
        {"id": "some/free-model", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "some/broken-model", "pricing": {"prompt": "free"}},
        {"id": "some/priceless-model"},
        "not-a-dict",
    ]
}


@pytest.fixture(autouse=True)
def clean_cache():
    pricing.reset()
    yield
    pricing.reset()


def client_serving(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(payload=CATALOGUE):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == pricing.MODELS_URL
        return httpx.Response(200, json=payload)

    return client_serving(handler)


def test_reads_a_models_per_token_price():
    price = pricing.price_of("openai/gpt-4o-mini", client=ok())

    assert price.prompt == pytest.approx(1.5e-7)
    assert price.completion == pytest.approx(6e-7)


def test_cost_of_multiplies_both_sides():
    price = pricing.price_of("openai/gpt-4o-mini", client=ok())

    assert price.cost_of(prompt_tokens=1_000_000, completion_tokens=1_000_000) == pytest.approx(
        0.75
    )


def test_a_free_model_keeps_its_zero_price():
    """`"0"` is a real price. Treating it as missing would make a free model
    report "estimate unavailable" instead of "$0.00"."""
    price = pricing.price_of("some/free-model", client=ok())

    assert price is not None
    assert price.prompt == 0.0


def test_a_model_with_unparseable_pricing_is_a_miss_not_a_crash():
    assert pricing.price_of("some/broken-model", client=ok()) is None


def test_a_model_absent_from_the_catalogue_is_none():
    assert pricing.price_of("anthropic/claude-sonnet-4.5", client=ok()) is None


def test_the_catalogue_is_fetched_once_per_process():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=CATALOGUE)

    client = client_serving(handler)
    pricing.price_of("openai/gpt-4o-mini", client=client)
    pricing.price_of("some/free-model", client=client)

    assert len(calls) == 1


def test_an_unreachable_catalogue_returns_none_and_records_why():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    assert pricing.price_of("openai/gpt-4o-mini", client=client_serving(handler)) is None
    assert "ConnectError" in pricing.failure_reason()


def test_an_http_error_returns_none_and_records_why():
    def handler(request):
        return httpx.Response(503, text="down")

    assert pricing.price_of("openai/gpt-4o-mini", client=client_serving(handler)) is None
    assert pricing.failure_reason()


def test_a_catalogue_without_a_data_list_returns_none_and_records_why():
    def handler(request):
        return httpx.Response(200, json={"error": "nope"})

    assert pricing.price_of("openai/gpt-4o-mini", client=client_serving(handler)) is None
    assert "data" in pricing.failure_reason()


def test_refresh_refetches():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=CATALOGUE)

    client = client_serving(handler)
    pricing.price_of("openai/gpt-4o-mini", client=client)
    pricing.price_of("openai/gpt-4o-mini", client=client, refresh=True)

    assert len(calls) == 2
