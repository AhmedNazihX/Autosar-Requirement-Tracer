from core.config import Settings


def test_settings_loads_without_openrouter_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Isolate from any real .env file on disk during this test.
    settings = Settings(_env_file=None)
    assert settings.openrouter_api_key is None


def test_max_report_cost_usd_is_unset_rather_than_defaulted(monkeypatch) -> None:
    """`None`, not 2.0 — the ceiling lives in two places.

    It is configured both here and in the manifest's
    `limits.max_report_cost_usd`, and `engines.report.ceiling_usd` gives this
    one precedence *when it is set*. A numeric default would make an unset
    environment variable silently override a manifest that said otherwise,
    which is the kind of precedence bug that costs money rather than a test.
    """
    monkeypatch.delenv("MAX_REPORT_COST_USD", raising=False)
    settings = Settings(_env_file=None)
    assert settings.max_report_cost_usd is None


def test_max_report_cost_usd_is_read_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("MAX_REPORT_COST_USD", "0.25")
    assert Settings(_env_file=None).max_report_cost_usd == 0.25


def test_settings_env_var_names_are_case_insensitive(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    settings = Settings(_env_file=None)
    assert settings.openrouter_api_key == "sk-test"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
