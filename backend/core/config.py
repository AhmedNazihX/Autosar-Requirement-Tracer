"""Application settings, loaded via pydantic-settings.

``OPENROUTER_API_KEY`` must stay optional at import time: the app has to
boot without it so the first-run setup screen (a later story) can detect
the missing key and guide the user. Never raise on a missing key here.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    openrouter_api_key: str | None = None

    #: The report cost hard stop (spec §11). ``None`` means "not set here",
    #: which is a different statement from "$2" and has to stay distinguishable:
    #: the ceiling is configured in *two* places — this environment variable
    #: and the manifest's ``limits.max_report_cost_usd`` — and
    #: :func:`engines.report.ceiling_usd` gives the environment precedence when
    #: it is set. A plain ``2.0`` default here would make an unset variable
    #: silently override a manifest that said something else.
    #: ``.env.example`` ships it set to 2.0, and the manifest agrees, so the
    #: effective default is unchanged.
    max_report_cost_usd: float | None = None

    #: The ``POST /chat`` token bucket (story S6.3.1). The sustained rate a
    #: runaway client is allowed, and how many requests may arrive back to
    #: back before it binds. Defaults chosen against a *person*: nobody types
    #: six questions in a row, and one every five seconds sustained is already
    #: faster than an agent turn completes.
    chat_rate_limit_per_minute: float = 12.0
    chat_rate_limit_burst: float = 6.0

    #: Which corpus this backend serves. A path, not a project name, because
    #: every corpus-specific value (model IDs, globs, the pinned SHA) lives in
    #: that manifest — see ``core/manifest.py``. Relative to ``backend/``,
    #: which is where the server is started from.
    project_manifest: str = "../projects/autosar-can/project.yaml"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
