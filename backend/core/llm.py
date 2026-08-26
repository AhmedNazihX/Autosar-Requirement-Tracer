"""OpenRouter chat models, via LangChain (WP2's three LLM stages, then WP3).

LangChain is the client — it is the locked stack (CLAUDE.md), and WP3's
tool-calling agent will use the very same :class:`~langchain_openai.ChatOpenAI`
instance this module builds. So this module is glue, not a client: it adds
only what LangChain does not do for us.

**1. Model IDs come from the manifest, never from code.** :func:`chat_model`
takes a *purpose* (``chat``, ``judge``, ``rerank``, ``title``) and resolves it
against ``models`` in ``project.yaml``. ``embedding`` is deliberately not a
purpose here — embeddings go through :mod:`core.embeddings`, which owns the
content-hash cache, and asking for one here is a mistake worth an error.

**2. Cost is OpenRouter's own figure** (finding D1: do not build a price
table). Two details make that work, and both are easy to lose:

* it must be *requested* — ``extra_body={"usage": {"include": True}}``, or the
  field is simply absent from every reply;
* it arrives in ``response_metadata["token_usage"]["cost"]``, because
  langchain-openai copies the provider's raw usage dict there verbatim, while
  its normalised ``usage_metadata`` carries only token counts.

**3. Structured output is validated here, not by the SDK.** This one was
measured while building the module and is not obvious. If ``response_format``
is set to a Pydantic *class* (which is what
``with_structured_output(method="json_schema")`` does), the ``openai`` SDK
validates the reply inside its own ``.parse()`` post-parser and raises
``ValidationError`` before LangChain's output parser ever runs — so
``include_raw=True`` does **not** hand back a ``parsing_error``, and the
response is lost, along with the cost of the call that produced it. Therefore
:meth:`Llm.structured` sends the JSON Schema as a plain **dict** (same wire
payload, no SDK-side validation), keeps the raw message, and validates with
Pydantic here. That buys two things the stages need: a retry that feeds the
validation error back to the model, and honest accounting for attempts that
were paid for and thrown away.

Capability separation (spec §8) is a property of how a model is *built*: the
judge and the reranker are constructed here and never given tools, and
:attr:`Llm.is_tool_less` lets story S6.1.2 assert that rather than trust it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import openai
from langchain_core.exceptions import LangChainException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from core.config import Settings, get_settings
from core.manifest import ProjectManifest

#: OpenAI-compatible endpoint. Locked by CLAUDE.md; not configurable per call.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: The environment variable and the file it belongs in, named in every error
#: about a missing key so the message is actionable on its own.
API_KEY_ENV_VAR = "OPENROUTER_API_KEY"
API_KEY_ENV_FILE = "backend/.env"
API_KEY_ENV_EXAMPLE = "backend/.env.example"

#: The purposes a chat model can be built for — each one a field of the
#: manifest's ``models`` block. ``embedding`` is excluded on purpose: it is
#: not a chat model and it belongs to :mod:`core.embeddings`.
PURPOSES: tuple[str, ...] = ("chat", "judge", "rerank", "title")

#: Asks OpenRouter to report what the call actually cost (finding D1).
USAGE_EXTRA_BODY: dict[str, Any] = {"usage": {"include": True}}

#: Deterministic by default. Every WP2 stage is a reproducible transformation
#: of its input, and a sampling temperature would make the pipeline's own
#: tests flaky before it made any answer better.
DEFAULT_TEMPERATURE = 0.0

DEFAULT_TIMEOUT_SECONDS = 120.0

#: Retries inside the ``openai`` SDK (429s, connection resets, 5xx). Two is
#: the SDK's own default and enough for a transient blip; a long ingestion-like
#: grind is not what a chat call is.
DEFAULT_MAX_RETRIES = 2

#: Attempts at getting schema-valid JSON. The second attempt is sent with the
#: validation error fed back, which is the one that usually succeeds; a third
#: rarely adds anything a caller's fallback would not handle better.
DEFAULT_MAX_STRUCTURED_ATTEMPTS = 2

#: ```` ```json … ``` ```` around an otherwise fine object. Models do this even
#: when asked for raw JSON, and re-prompting for it would be a wasted call.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)

#: Errors LangChain and the SDK raise for a provider failure. The LangChain
#: subclasses inherit from both families, so both are needed to catch the set.
_PROVIDER_ERRORS = (openai.OpenAIError, LangChainException)


class LlmError(Exception):
    """A chat call failed, or cannot be attempted.

    ``usage`` carries what the failed attempts cost when that is known. A
    caller with a fallback path (the reranker returns RRF order, story
    S2.5.1) still has to report the money it spent getting nowhere.
    """

    def __init__(self, message: str, *, usage: LlmUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def system(content: str) -> SystemMessage:
    """A system message. Named for readability at the call sites."""
    return SystemMessage(content=content)


def user(content: str) -> HumanMessage:
    """A user message."""
    return HumanMessage(content=content)


# --------------------------------------------------------------------------
# usage accounting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmUsage:
    """What a set of chat calls consumed, tagged with its purpose.

    ``purpose`` is the string story S3.1.1's cost meter aggregates on, and
    :meth:`plus` refuses to mix two of them — a judge's spend folded into the
    reranker's line would misreport both and there is no way to notice later.
    """

    purpose: str = "chat"
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    @classmethod
    def of(cls, message: BaseMessage, *, purpose: str) -> LlmUsage:
        """Read one reply's usage, preferring OpenRouter's own cost figure.

        Tolerates a reply with no usage at all (a provider that omits the
        block, or a hand-built message in a test) rather than raising: an
        unreported cost is a reporting gap, not a failed call.
        """
        metadata = getattr(message, "usage_metadata", None) or {}
        raw_usage = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
        prompt = int(metadata.get("input_tokens", 0) or 0)
        completion = int(metadata.get("output_tokens", 0) or 0)
        return cls(
            purpose=purpose,
            calls=1,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(metadata.get("total_tokens", 0) or 0) or prompt + completion,
            cost_usd=float(raw_usage.get("cost") or 0.0),
        )

    def plus(self, other: LlmUsage) -> LlmUsage:
        """Add ``other`` to this. Both must share a purpose."""
        if self.purpose != other.purpose:
            raise ValueError(
                f"cannot add usage for purpose {other.purpose!r} to usage for "
                f"{self.purpose!r} — aggregate per purpose, then report each separately"
            )
        return replace(
            self,
            calls=self.calls + other.calls,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass(frozen=True)
class Completion:
    """A plain text reply, and what it cost."""

    text: str
    usage: LlmUsage


@dataclass(frozen=True)
class Structured[T: BaseModel]:
    """A validated reply, what it cost, and how many attempts it took.

    ``attempts`` is worth returning rather than logging: a stage that needed
    a retry to get valid JSON is a signal about the prompt, and RAGAS-era
    instrumentation (story S2.6.1) wants it per stage.
    """

    value: T
    usage: LlmUsage
    attempts: int


# --------------------------------------------------------------------------
# building a model
# --------------------------------------------------------------------------


def _resolve_key(api_key: str | None, settings: Settings | None) -> str:
    if api_key:
        return api_key
    resolved = settings if settings is not None else get_settings()
    key = (resolved.openrouter_api_key or "").strip()
    if not key:
        raise LlmError(
            f"{API_KEY_ENV_VAR} is not set, so no model call can be made. Put a key in "
            f"{API_KEY_ENV_FILE} (copy {API_KEY_ENV_EXAMPLE} and fill in "
            f"{API_KEY_ENV_VAR}=sk-or-...)."
        )
    return key


def _model_id(manifest: ProjectManifest, purpose: str) -> str:
    if purpose not in PURPOSES:
        allowed = ", ".join(PURPOSES)
        extra = ""
        if purpose in {"embedding", "embed"}:
            extra = (
                " — embedding models are not chat models; use core.embeddings.client_for, "
                "which owns the content-hash cache"
            )
        raise ValueError(f"unknown model purpose {purpose!r}; expected one of {allowed}{extra}")
    return getattr(manifest.models, purpose)


def chat_model(
    manifest: ProjectManifest,
    purpose: str,
    *,
    settings: Settings | None = None,
    api_key: str | None = None,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    **kwargs: Any,
) -> Llm:
    """An :class:`Llm` for ``purpose``, on the manifest's pinned model.

    Constructed per use rather than held as module state, so a fresh clone can
    boot without a key and the first-run setup screen (story S3.6.1) can ask
    for one. Extra ``kwargs`` reach ``ChatOpenAI`` (``http_client`` is how the
    tests intercept without a network).
    """
    model_id = _model_id(manifest, purpose)
    model = ChatOpenAI(
        model=model_id,
        api_key=_resolve_key(api_key, settings),
        base_url=OPENROUTER_BASE_URL,
        temperature=temperature,
        timeout=timeout,
        max_retries=max_retries,
        extra_body=dict(USAGE_EXTRA_BODY),
        **kwargs,
    )
    return Llm(purpose=purpose, model_id=model_id, model=model)


# --------------------------------------------------------------------------
# the wrapper
# --------------------------------------------------------------------------


def _schema_response_format(schema: type[BaseModel]) -> dict[str, Any]:
    """A ``response_format`` carrying ``schema`` as a plain dict.

    A dict rather than the Pydantic class on purpose — see the module
    docstring: passing the class makes the SDK validate and discard the
    response, and with it the ability to retry or to bill for the attempt.

    ``strict`` is off because OpenAI's strict subset requires every property
    to be required, which no schema with an optional field can satisfy (the
    self-query filter, story S2.4.1, is entirely optional fields). The schema
    still steers generation; correctness is guaranteed by validating here.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": schema.__name__,
            "strict": False,
            "schema": schema.model_json_schema(),
        },
    }


def _unfence(text: str) -> str:
    """Strip a Markdown code fence, if the whole reply is wrapped in one."""
    match = _FENCE.match(text)
    return match.group("body") if match else text


def _text_of(message: BaseMessage) -> str:
    """The reply's text, whether the provider sent a string or content blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = [
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


@dataclass(frozen=True)
class Llm:
    """A chat model, the purpose it was built for, and its pinned model id.

    Holds the LangChain model rather than hiding it: WP3's agent needs the
    real :class:`BaseChatModel` to bind tools onto, and re-wrapping it would
    mean two ways to reach OpenRouter again.
    """

    purpose: str
    model_id: str
    model: BaseChatModel

    @property
    def is_tool_less(self) -> bool:
        """True when no tool is bound — spec §8's capability separation.

        ``bind_tools`` returns a ``RunnableBinding`` carrying them in
        ``kwargs``, so this reads that rather than trusting the call site.
        Story S6.1.2 asserts on it for the judge and the reranker.
        """
        return not (getattr(self.model, "kwargs", None) or {}).get("tools")

    def complete(self, messages: Sequence[BaseMessage], **kwargs: Any) -> Completion:
        """One call, returning the reply text and its usage."""
        message = self._invoke(messages, **kwargs)
        return Completion(text=_text_of(message), usage=LlmUsage.of(message, purpose=self.purpose))

    def structured[T: BaseModel](
        self,
        schema: type[T],
        messages: Sequence[BaseMessage],
        *,
        max_attempts: int = DEFAULT_MAX_STRUCTURED_ATTEMPTS,
        **kwargs: Any,
    ) -> Structured[T]:
        """A reply validated against ``schema``, retried with the error fed back.

        The retry is not a bare re-send: the model's own bad reply and the
        validation error go back with it, which is what makes a second attempt
        worth paying for. Every attempt's usage is accumulated, including the
        discarded ones and including the path that ends in :class:`LlmError`.
        """
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be at least 1, got {max_attempts}")

        conversation = list(messages)
        usage = LlmUsage(purpose=self.purpose)
        response_format = _schema_response_format(schema)
        last_error = ""

        for attempt in range(1, max_attempts + 1):
            try:
                message = self._invoke(conversation, response_format=response_format, **kwargs)
            except LlmError as exc:
                raise LlmError(str(exc), usage=usage) from exc
            usage = usage.plus(LlmUsage.of(message, purpose=self.purpose))
            text = _unfence(_text_of(message))
            try:
                value = schema.model_validate_json(text)
            except ValidationError as exc:
                last_error = _describe(exc)
                if attempt == max_attempts:
                    break
                # The model's own bad reply plus the error goes back with the
                # retry — a bare re-send of the same prompt usually returns the
                # same wrong answer, which is a call paid for and wasted.
                conversation = [
                    *conversation,
                    AIMessage(content=text),
                    user(
                        f"That reply is not valid {schema.__name__}: {last_error}\n"
                        "Reply again with JSON matching the schema exactly, and nothing else."
                    ),
                ]
            else:
                return Structured(value=value, usage=usage, attempts=attempt)

        raise LlmError(
            f"{self.model_id} did not return valid {schema.__name__} in {max_attempts} "
            f"attempt(s) — {last_error}",
            usage=usage,
        )

    def _invoke(self, messages: Sequence[BaseMessage], **kwargs: Any) -> BaseMessage:
        if not messages:
            raise LlmError("a chat call needs at least one message, got none")
        model = self.model.bind(**kwargs) if kwargs else self.model
        try:
            return model.invoke(list(messages))
        except _PROVIDER_ERRORS as exc:
            raise LlmError(f"{self.model_id} ({self.purpose}) call failed — {exc}") from exc


def _describe(exc: ValidationError) -> str:
    """A short, model-readable summary of what was wrong with the JSON."""
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error["loc"]) or "(root)"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts) or str(exc)
