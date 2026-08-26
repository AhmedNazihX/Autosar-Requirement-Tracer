"""Tests for the thin OpenRouter glue over LangChain's chat model.

LangChain is the client (CLAUDE.md's locked stack); :mod:`core.llm` adds only
the three things it will not do for us, and each one is here because it fails
silently otherwise:

* **Model IDs come from the manifest and nowhere else.** A hard-coded id would
  work perfectly until someone re-pins a model and half the pipeline kept
  using the old one.
* **Cost is OpenRouter's own ``usage.cost``** (finding D1: do not build a price
  table). It has to be *asked for* on the wire, and it arrives in
  ``response_metadata``, not in LangChain's normalised ``usage_metadata`` — so
  both halves are pinned down here.
* **Structured output is validated locally, retried with the error fed back,
  and every attempt is still billed.** Verified while building this: with
  ``response_format`` set to a Pydantic class, the ``openai`` SDK validates
  the reply itself and raises before LangChain's parser sees it, taking the
  response — and its cost — with it. So the schema goes over the wire as a
  plain dict and validation happens here, where a failure keeps the message.

No test reaches OpenRouter: :mod:`tests.support_llm` intercepts at the HTTP
layer, so an unscripted call raises rather than escaping to the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field

from core.config import Settings
from core.llm import (
    API_KEY_ENV_FILE,
    API_KEY_ENV_VAR,
    OPENROUTER_BASE_URL,
    PURPOSES,
    Llm,
    LlmError,
    LlmUsage,
    chat_model,
    system,
    user,
)
from core.manifest import load_manifest
from tests.support_llm import fake_llm, json_body, openrouter_body

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
MANIFEST = load_manifest(REPO_ROOT / "projects" / "autosar-can" / "project.yaml")


class Rewrites(BaseModel):
    """A stand-in for the typed payloads WP2's stages actually ask for."""

    model_config = ConfigDict(extra="forbid")

    queries: list[str] = Field(min_length=1)


def keyless_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """A ``Settings`` with no key, ignoring the developer's real ``.env``."""
    monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)
    return Settings(_env_file=None)


# --------------------------------------------------------------------------
# the manifest is the only source of a model id
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("purpose", "expected"),
    [
        ("chat", "anthropic/claude-sonnet-4.5"),
        ("judge", "openai/gpt-4o-mini"),
        ("rerank", "openai/gpt-4o-mini"),
        ("translate", "openai/gpt-4o-mini"),
        ("title", "openai/gpt-4o-mini"),
    ],
)
def test_the_model_id_comes_from_the_manifest(purpose: str, expected: str):
    llm, _ = fake_llm(MANIFEST, purpose=purpose)
    assert llm.model_id == expected
    assert llm.purpose == purpose


def test_every_named_purpose_is_a_manifest_model_field():
    """Guards against a purpose that silently resolves to nothing."""
    for purpose in PURPOSES:
        assert isinstance(getattr(MANIFEST.models, purpose), str)


def test_embedding_is_not_a_chat_purpose():
    """Embeddings go through core.embeddings; naming it here is a mistake."""
    with pytest.raises(ValueError, match="embedding"):
        chat_model(MANIFEST, "embedding", api_key="sk-test")


def test_an_unknown_purpose_names_the_valid_ones():
    with pytest.raises(ValueError) as error:
        chat_model(MANIFEST, "summarise", api_key="sk-test")
    assert "summarise" in str(error.value)
    assert "rerank" in str(error.value), "the message must list what is allowed"


def test_the_model_is_pointed_at_openrouter():
    llm, _ = fake_llm(MANIFEST, purpose="rerank")
    assert llm.model.openai_api_base == OPENROUTER_BASE_URL


# --------------------------------------------------------------------------
# cost: OpenRouter's own figure (finding D1)
# --------------------------------------------------------------------------


def test_the_request_asks_openrouter_to_report_cost():
    """Without this, ``usage.cost`` is simply absent from every reply."""
    llm, fake = fake_llm(MANIFEST, openrouter_body("ok"))
    llm.complete([user("hi")])
    assert fake.requests[0]["usage"] == {"include": True}


def test_usage_reports_the_providers_cost_and_is_tagged_with_the_purpose():
    llm, _ = fake_llm(
        MANIFEST,
        openrouter_body("ok", cost_usd=0.00042, prompt_tokens=120, completion_tokens=30),
        purpose="rerank",
    )

    result = llm.complete([user("hi")])

    assert result.usage.purpose == "rerank"
    assert result.usage.cost_usd == pytest.approx(0.00042)
    assert result.usage.prompt_tokens == 120
    assert result.usage.completion_tokens == 30
    assert result.usage.total_tokens == 150
    assert result.usage.calls == 1


def test_a_reply_without_a_cost_field_is_zero_not_a_crash():
    """Some models on OpenRouter report no cost; that is not an error."""
    body = openrouter_body("ok", prompt_tokens=5)
    del body["usage"]["cost"]
    llm, _ = fake_llm(MANIFEST, body)

    usage = llm.complete([user("hi")]).usage

    assert usage.cost_usd == 0.0
    assert usage.prompt_tokens == 5


def test_usage_of_a_message_with_no_usage_at_all_is_zero():
    assert LlmUsage.of(AIMessage(content="x"), purpose="judge") == LlmUsage(
        purpose="judge", calls=1
    )


def test_usage_adds_up_across_stages():
    """The pipeline sums per-stage usage into one per-message figure (S3.1.1)."""
    first = LlmUsage(purpose="rerank", calls=1, prompt_tokens=10, cost_usd=0.001)
    second = LlmUsage(purpose="rerank", calls=1, prompt_tokens=5, cost_usd=0.002)

    total = first.plus(second)

    assert total.calls == 2
    assert total.prompt_tokens == 15
    assert total.cost_usd == pytest.approx(0.003)
    assert total.purpose == "rerank"


def test_adding_usage_of_two_purposes_is_refused():
    """Silently merging a judge's cost into the reranker's would misreport both."""
    with pytest.raises(ValueError, match="purpose"):
        LlmUsage(purpose="judge").plus(LlmUsage(purpose="rerank"))


# --------------------------------------------------------------------------
# plain completions
# --------------------------------------------------------------------------


def test_a_completion_returns_the_assistant_text():
    llm, _ = fake_llm(MANIFEST, openrouter_body("bus-off goes to CanIf_ControllerBusOff"))
    assert llm.complete([user("How is bus-off reported?")]).text == (
        "bus-off goes to CanIf_ControllerBusOff"
    )


def test_the_messages_reach_the_wire_with_their_roles():
    llm, fake = fake_llm(MANIFEST, openrouter_body("ok"))

    llm.complete([system("be terse"), user("SWS_Can_00011")])

    assert [(m["role"], m["content"]) for m in fake.messages_of(0)] == [
        ("system", "be terse"),
        ("user", "SWS_Can_00011"),
    ]


def test_an_empty_message_list_is_refused_before_any_request():
    llm, fake = fake_llm(MANIFEST)
    with pytest.raises(LlmError, match="at least one message"):
        llm.complete([])
    assert fake.calls == 0, "no request may go out"


def test_temperature_defaults_to_zero_for_reproducibility():
    """Every WP2 stage is deterministic by design; a default of 0.7 is not."""
    llm, fake = fake_llm(MANIFEST, openrouter_body("ok"))
    llm.complete([user("hi")])
    assert fake.requests[0]["temperature"] == 0.0


# --------------------------------------------------------------------------
# structured output
# --------------------------------------------------------------------------


def test_structured_output_returns_a_validated_model():
    llm, _ = fake_llm(MANIFEST, json_body({"queries": ["bus off", "error passive"]}))

    result = llm.structured(Rewrites, [user("expand: bus off")])

    assert isinstance(result.value, Rewrites)
    assert result.value.queries == ["bus off", "error passive"]
    assert result.attempts == 1


def test_structured_output_sends_the_schema_as_a_plain_dict():
    """A Pydantic class here would make the SDK validate — and swallow the cost."""
    llm, fake = fake_llm(MANIFEST, json_body({"queries": ["a"]}))

    llm.structured(Rewrites, [user("expand: a")])

    response_format = fake.requests[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "Rewrites"
    assert "queries" in response_format["json_schema"]["schema"]["properties"]


def test_json_wrapped_in_a_code_fence_is_still_read():
    """Providers do this even when asked for JSON; it is not worth a retry."""
    llm, fake = fake_llm(MANIFEST, openrouter_body('```json\n{"queries": ["a"]}\n```'))
    assert llm.structured(Rewrites, [user("x")]).value.queries == ["a"]
    assert fake.calls == 1, "a fenced reply must not cost a retry"


def test_a_schema_violation_is_retried_with_the_validation_error_fed_back():
    llm, fake = fake_llm(
        MANIFEST,
        json_body({"queries": []}),  # violates min_length=1
        json_body({"queries": ["bus off"]}),
    )

    result = llm.structured(Rewrites, [user("expand: bus off")])

    assert result.value.queries == ["bus off"]
    assert result.attempts == 2
    retry_prompt = fake.prompt_of(1)
    assert "queries" in retry_prompt, "the retry must name the field that failed"
    assert "expand: bus off" in retry_prompt, "and must keep the original request"


def test_unparseable_output_is_retried_then_raises_naming_the_schema():
    llm, fake = fake_llm(
        MANIFEST,
        openrouter_body("I'm afraid I can't do that"),
        openrouter_body("still not JSON"),
    )

    with pytest.raises(LlmError, match="Rewrites"):
        llm.structured(Rewrites, [user("x")], max_attempts=2)
    assert fake.calls == 2


def test_the_cost_of_discarded_attempts_is_still_reported():
    """A malformed reply was paid for; a cost meter that hides it is wrong."""
    llm, _ = fake_llm(
        MANIFEST,
        openrouter_body("not json", cost_usd=0.002),
        json_body({"queries": ["a"]}, cost_usd=0.003),
    )

    result = llm.structured(Rewrites, [user("x")])

    assert result.usage.cost_usd == pytest.approx(0.005)
    assert result.usage.calls == 2


def test_the_cost_of_a_failed_structured_call_reaches_the_caller():
    """The reranker falls back to RRF order (S2.5.1) but still owes the money."""
    llm, _ = fake_llm(
        MANIFEST,
        openrouter_body("nope", cost_usd=0.002),
        openrouter_body("nope", cost_usd=0.002),
    )

    with pytest.raises(LlmError) as error:
        llm.structured(Rewrites, [user("x")], max_attempts=2)

    assert error.value.usage is not None
    assert error.value.usage.cost_usd == pytest.approx(0.004)


# --------------------------------------------------------------------------
# capability separation (spec §8, story S6.1.2)
# --------------------------------------------------------------------------


def test_a_freshly_built_model_has_no_tools_bound():
    """The judge and the reranker must never be able to act."""
    llm, _ = fake_llm(MANIFEST, purpose="judge")
    assert llm.is_tool_less


def test_binding_a_tool_makes_the_wrapper_report_it():
    """The assertion must be able to fail, or it proves nothing."""
    llm, _ = fake_llm(MANIFEST, purpose="judge")
    armed = Llm(
        purpose=llm.purpose,
        model_id=llm.model_id,
        model=llm.model.bind_tools([{"name": "search_code", "parameters": {}}]),
    )
    assert not armed.is_tool_less


def test_a_structured_call_sends_no_tools_on_the_wire():
    llm, fake = fake_llm(MANIFEST, json_body({"queries": ["a"]}), purpose="judge")
    llm.structured(Rewrites, [user("x")])
    assert "tools" not in fake.requests[0]


# --------------------------------------------------------------------------
# transport failures
# --------------------------------------------------------------------------


def test_a_429_is_retried_by_the_client():
    llm, fake = fake_llm(
        MANIFEST,
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        openrouter_body("ok"),
    )
    assert llm.complete([user("x")]).text == "ok"
    assert fake.calls == 2


def test_a_persistent_server_error_surfaces_as_an_llm_error():
    llm, _ = fake_llm(
        MANIFEST,
        *[httpx.Response(503, json={"error": {"message": "unavailable"}})] * 6,
        max_retries=1,
    )
    with pytest.raises(LlmError):
        llm.complete([user("x")])


# --------------------------------------------------------------------------
# the missing key
# --------------------------------------------------------------------------


def test_a_missing_key_names_the_variable_and_the_file(monkeypatch):
    with pytest.raises(LlmError) as error:
        chat_model(MANIFEST, "rerank", settings=keyless_settings(monkeypatch))

    message = str(error.value)
    assert API_KEY_ENV_VAR in message
    assert API_KEY_ENV_FILE in message


def test_importing_the_module_without_a_key_does_not_raise():
    """A fresh clone must boot so the first-run screen can ask for the key."""
    environment = {key: value for key, value in os.environ.items() if key != API_KEY_ENV_VAR}
    environment["PYTHONPATH"] = str(BACKEND_DIR)
    completed = subprocess.run(
        [sys.executable, "-c", "import core.llm; print('imported')"],
        cwd=str(BACKEND_DIR),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "imported" in completed.stdout
