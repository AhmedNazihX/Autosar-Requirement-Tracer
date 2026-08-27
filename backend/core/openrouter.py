"""The one place that knows how this project reaches OpenRouter.

Every LLM, embedding and rerank call goes through OpenRouter's
OpenAI-compatible endpoint (locked by CLAUDE.md), and every module that makes
one needs the same three things: the base URL, the configured key, and an
actionable sentence when the key is missing. ``core.llm`` and
``core.embeddings`` each had their own copy of all three — same base URL,
same env-var names, near-identical error prose — which is exactly the kind of
duplication that drifts. They now share this module and keep only what
genuinely differs: their exception types and what a caller should do instead.
"""

from __future__ import annotations

from core.config import Settings, get_settings

#: OpenAI-compatible endpoint. Locked by CLAUDE.md; not configurable per call.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

#: The environment variable and the file it belongs in, named in every error
#: about a missing key so the message is actionable on its own.
API_KEY_ENV_VAR = "OPENROUTER_API_KEY"
API_KEY_ENV_FILE = "backend/.env"
API_KEY_ENV_EXAMPLE = "backend/.env.example"


def configured_key(settings: Settings | None = None) -> str:
    """The configured OpenRouter key, stripped — ``""`` when unset.

    Read at call time only, never at import, so a fresh clone with no key can
    still boot the API and run ingestion with ``--skip-embeddings``. Callers
    turn an empty result into their own exception type via
    :func:`missing_key_message`.
    """
    resolved = settings if settings is not None else get_settings()
    return (resolved.openrouter_api_key or "").strip()


def missing_key_message(consequence: str, *, alternative: str = "") -> str:
    """One sentence saying what cannot happen and how to fix it.

    ``consequence`` completes "``OPENROUTER_API_KEY`` is not set, so …";
    ``alternative`` (", or re-run with --skip-embeddings …") lets a caller
    offer a keyless way out.
    """
    return (
        f"{API_KEY_ENV_VAR} is not set, so {consequence}. Put a key in "
        f"{API_KEY_ENV_FILE} (copy {API_KEY_ENV_EXAMPLE} and fill in "
        f"{API_KEY_ENV_VAR}=sk-or-...){alternative}."
    )
