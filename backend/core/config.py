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
    max_report_cost_usd: float = 2.0

    #: Which corpus this backend serves. A path, not a project name, because
    #: every corpus-specific value (model IDs, globs, the pinned SHA) lives in
    #: that manifest — see ``core/manifest.py``. Relative to ``backend/``,
    #: which is where the server is started from.
    project_manifest: str = "../projects/autosar-can/project.yaml"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()
